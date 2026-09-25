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
// Intermediate kinds are never nodes: the major-only palette above keeps
// rejecting them as items, and they validate as owner-tagged details only.
const detailKinds = { equation: 'Equation', claim: 'Claim', derivation: 'Derivation' };

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
  if (!input || input.schema_version !== 3) throw new Error('Prepared input requires schema_version 3.');
  if (!Array.isArray(input.items) || !Array.isArray(input.uses) || !input.items.length) {
    throw new Error('Prepared input needs nonempty items and a uses array.');
  }
  if (!Array.isArray(input.details) || !Array.isArray(input.detail_uses)) {
    throw new Error('Prepared input needs details and detail_uses arrays, empty when no intermediate steps are recorded.');
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
    if (!use || !ids.has(use.from) || !ids.has(use.to) || !use.reason) {
      throw new Error(`Dependency ${index + 1} needs valid endpoints and a reason.`);
    }
    const id = use.id || `use-${index + 1}`;
    if (!/^[a-zA-Z][a-zA-Z0-9_.:-]*$/.test(id) || useIds.has(id)) throw new Error('Dependency identifiers must be safe and unique.');
    useIds.add(id);
    const type = use.type || 'dependency';
    if (!Object.hasOwn(useTypes, type)) throw new Error(`Dependency ${id} has an unsupported type.`);
    if (use.regime !== undefined && (typeof use.regime !== 'string' || !use.regime.trim())) throw new Error(`Dependency ${id} needs a nonempty regime.`);
    return { ...use, id, type };
  });
  const detailIds = new Set();
  const details = input.details.map((detail) => {
    if (!detail || !/^[a-zA-Z][a-zA-Z0-9_.:-]*$/.test(detail.id || '') || ids.has(detail.id) || detailIds.has(detail.id)) {
      throw new Error('Each detail needs a unique safe identifier distinct from every item.');
    }
    if (!Object.hasOwn(detailKinds, detail.kind) || !detail.label || typeof detail.statement_html !== 'string') {
      throw new Error(`Detail ${detail.id} needs an intermediate kind (equation, claim, or derivation), label, and prepared statement_html.`);
    }
    if (!ids.has(detail.owner)) throw new Error(`Detail ${detail.id} needs an owner naming an existing major item.`);
    detailIds.add(detail.id);
    return detail;
  });
  details.sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  const endpoints = new Set([...ids, ...detailIds]);
  const detailUses = input.detail_uses.map((use, index) => {
    // An intermediate row may reference itself: the edge stays an annotation
    // in its owner's panel and never enters the graph. The major-major check
    // below keeps every major-major reference in the visible graph.
    if (!use || !endpoints.has(use.from) || !endpoints.has(use.to) || !use.reason) {
      throw new Error(`Detail use ${index + 1} needs endpoints among items and details, and a reason.`);
    }
    if (ids.has(use.from) && ids.has(use.to)) {
      throw new Error(`Detail use ${use.id || index + 1} joins two major items; record it as an ordinary use.`);
    }
    const id = use.id || `detail-use-${index + 1}`;
    if (!/^[a-zA-Z][a-zA-Z0-9_.:-]*$/.test(id) || useIds.has(id)) throw new Error('Detail use identifiers must be safe and distinct from graph use identifiers.');
    useIds.add(id);
    const type = use.type || 'dependency';
    if (!Object.hasOwn(useTypes, type)) throw new Error(`Detail use ${id} has an unsupported type.`);
    if (use.regime !== undefined && (typeof use.regime !== 'string' || !use.regime.trim())) throw new Error(`Detail use ${id} needs a nonempty regime.`);
    return { ...use, id, type };
  });
  const groupKinds = new Map();
  for (const use of [...uses, ...detailUses]) {
    if (use.group === undefined || use.group === null) continue;
    if (typeof use.group.id !== 'string' || !use.group.id.trim() || !['joint', 'cases'].includes(use.group.kind)) {
      throw new Error(`Use ${use.id} needs a group with a name and kind joint or cases.`);
    }
    const prior = groupKinds.get(use.group.id);
    if (prior !== undefined && prior !== use.group.kind) {
      throw new Error(`Group ${use.group.id} mixes ${prior} and ${use.group.kind}; one group id keeps one consistent kind.`);
    }
    groupKinds.set(use.group.id, use.group.kind);
  }
  if (input.main_items !== undefined && (!Array.isArray(input.main_items) || !input.main_items.length || new Set(input.main_items).size !== input.main_items.length || input.main_items.some((id) => !ids.has(id)))) throw new Error('main_items must contain distinct existing item identifiers.');
  if (input.graph_mode !== undefined && !['dag', 'cyclic', 'index'].includes(input.graph_mode)) throw new Error('graph_mode must be dag, cyclic or index.');
  if (input.math_diagnostics !== undefined && (!Array.isArray(input.math_diagnostics) || input.math_diagnostics.some((entry) => !entry || typeof entry.id !== 'string' || typeof entry.field !== 'string' || typeof entry.reason !== 'string' || typeof entry.excerpt !== 'string'))) throw new Error('math_diagnostics entries need id, field, excerpt, and reason strings.');
  return { ...input, uses, details, detail_uses: detailUses };
}

// Detail rows ride along with both graph shapes so panels can render them
// without touching layout: detailsByOwner is id-sorted, detailUses untouched.
function detailIndex(data) {
  const details = new Map((data.details || []).map((row) => [row.id, row]));
  const detailsByOwner = new Map(data.items.map((item) => [item.id, []]));
  for (const row of details.values()) detailsByOwner.get(row.owner).push(row);
  return { details, detailsByOwner, detailUses: data.detail_uses || [] };
}

function indexGraph(data) {
  const nodes = new Map(data.items.map((item) => [item.id, item]));
  const incoming = new Map(data.items.map((item) => [item.id, []]));
  const outgoing = new Map(data.items.map((item) => [item.id, []]));
  data.uses.forEach((use) => { incoming.get(use.to).push(use); outgoing.get(use.from).push(use); });
  return { nodes, incoming, outgoing, ...detailIndex(data) };
}

// Strongly connected groups determine layout ranks only. All authored nodes
// and directed uses remain separate; no edge is removed or reversed.
function stronglyConnected(nodes, incoming, outgoing) {
  const seen = new Set(), finished = [];
  for (const start of nodes.keys()) {
    if (seen.has(start)) continue;
    seen.add(start);
    const stack = [{ id: start, index: 0 }];
    while (stack.length) {
      const frame = stack.at(-1), uses = outgoing.get(frame.id);
      if (frame.index >= uses.length) { finished.push(frame.id); stack.pop(); continue; }
      const target = uses[frame.index++].to;
      if (!seen.has(target)) { seen.add(target); stack.push({ id: target, index: 0 }); }
    }
  }
  const assigned = new Set(), groups = [];
  for (const start of finished.reverse()) {
    if (assigned.has(start)) continue;
    const members = [start]; assigned.add(start);
    for (let index = 0; index < members.length; index += 1) {
      for (const use of incoming.get(members[index])) {
        if (!assigned.has(use.from)) { assigned.add(use.from); members.push(use.from); }
      }
    }
    members.sort((a, b) => nodes.get(a).order - nodes.get(b).order);
    groups.push({ members, rank: 0, incoming: 0, outgoing: [] });
  }
  return groups;
}

// DAGs retain their existing longest-path layers and stable barycentres.
// Cyclic groups expand into stable columns with exterior lanes for return uses.
function layoutGraph(data) {
  const nodes = new Map(data.items.map((item, order) => [item.id, { ...item, order, rank: 0 }]));
  const incoming = new Map(data.items.map((item) => [item.id, []]));
  const outgoing = new Map(data.items.map((item) => [item.id, []]));
  data.uses.forEach((use) => { incoming.get(use.to).push(use); outgoing.get(use.from).push(use); });
  const groups = stronglyConnected(nodes, incoming, outgoing), groupOf = new Map();
  groups.forEach((group) => group.members.forEach((id) => groupOf.set(id, group)));
  for (const use of data.uses) {
    const from = groupOf.get(use.from), to = groupOf.get(use.to);
    if (from !== to) { from.outgoing.push(to); to.incoming += 1; }
  }
  const queue = groups.filter((group) => !group.incoming);
  for (let index = 0; index < queue.length; index += 1) {
    const group = queue[index];
    group.members.forEach((id, offset) => { nodes.get(id).rank = group.rank + offset; });
    for (const target of group.outgoing) {
      target.rank = Math.max(target.rank, group.rank + group.members.length);
      target.incoming -= 1;
      if (!target.incoming) queue.push(target);
    }
  }
  const cycles = groups.filter((group) => group.members.length > 1 || outgoing.get(group.members[0]).some((use) => use.to === group.members[0])).map((group) => {
    const members = new Set(group.members);
    return { item_ids: group.members, item_labels: group.members.map((id) => nodes.get(id).label),
      use_ids: data.uses.filter((use) => members.has(use.from) && members.has(use.to)).map((use) => use.id) };
  }).sort((a, b) => nodes.get(a.item_ids[0]).order - nodes.get(b.item_ids[0]).order);
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
    const memberIds = new Set(ids);
    const returns = data.uses.filter((use) => memberIds.has(use.from) && nodes.get(use.to).rank <= nodes.get(use.from).rank);
    components.push({ members, ranks, returns, rowCount: Math.max(...ranks.map((rank) => rank.length)) });
  });
  const longUses = data.uses.filter((use) => nodes.get(use.to).rank > nodes.get(use.from).rank + 1);
  let top = box.margin;
  components.forEach((component, componentIndex) => {
    component.top = top;
    component.laneTop = top + (components.length > 1 ? 32 : 0);
    component.nodeTop = component.laneTop + (component.returns.length ? 24 + component.returns.length * 10 : 0);
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
    nodes, components, incoming, outgoing, longUses, cycles, ...detailIndex(data),
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
  if (b.rank <= a.rank) {
    const component = graph.components[a.component], lane = component.returns.indexOf(use);
    const rail = component.laneTop + 10 + lane * 10;
    const exit = start[0] + 12 + (outs.indexOf(use) + 1) / (outs.length + 1) * 12;
    const enter = end[0] - 12 - (ins.indexOf(use) + 1) / (ins.length + 1) * 12;
    return [start, [exit, start[1]], [exit, rail], [enter, rail], [enter, end[1]], end];
  }
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
  const cycleUses = new Set(graph.cycles.flatMap((group) => group.use_ids));
  const cycleItems = new Set(graph.cycles.flatMap((group) => group.item_ids));
  const edges = data.uses.map((use, index) => {
    const points = edgePoints(use, graph);
    const issued = Boolean(use.issue);
    const group = use.group || null;
    // R3 badge fallback: grouped uses keep their own paths and disclose the
    // group with a badge; the forked single-arrowhead connector spike is
    // deferred. Every use id therefore appears exactly once in the SVG.
    const qualification = `${useTypes[use.type]}${use.regime ? `; only in regime: ${use.regime}` : ''}${group ? `; ${group.kind === 'joint' ? 'required jointly' : 'alternative case'}: ${group.id}` : ''}${cycleUses.has(use.id) ? '; part of a recorded cycle' : ''}`;
    const description = `${graph.nodes.get(use.from).label} to ${graph.nodes.get(use.to).label}. ${qualification}: ${use.reason}${issued ? ` Issue: ${use.issue}` : ''}`;
    if (use.type !== 'dependency' || use.regime || group) {
      const labels = [use.type !== 'dependency' ? useTypes[use.type] : '', use.regime ? `If: ${use.regime}` : '', group ? `${group.kind === 'joint' ? 'Joint' : 'Case'}: ${group.id}` : ''].filter(Boolean).map((label) => textUnits(label) > 17 ? `${label.slice(0, 15)}…` : label);
      qualifiers.push({ use, description, labels, points });
    }
    return `<path data-edge-from="${esc(use.from)}" data-edge-to="${esc(use.to)}" data-edge-key="${index}" data-edge-id="${esc(use.id)}" data-use-type="${esc(use.type)}"${use.regime ? ` data-use-regime="${esc(use.regime)}"` : ''} data-edge-label="${esc(`${qualification}: ${use.reason}`)}" data-composition-points="${points.map((point) => point.join(',')).join(';')}" class="proof-edge a-default${issued ? ' proof-uncertain' : ''}${use.type === 'proof_argument' ? ' proof-argument-edge' : ''}" d="${roundedPath(points)}" stroke-width="1.6" marker-end="url(#proof-arrow)"><title>${esc(description)}</title></path>`;
  }).join('\n');
  const nodes = [...graph.nodes.values()].map((node) => {
    const caption = captionLines(node.caption), cx = node.x + box.w / 2;
    const labelSize = Math.max(12, Math.min(14, 150 / Math.max(1, textUnits(node.label)) / 0.61));
    const aria = `${node.label}. ${node.caption || kinds[node.kind][0]}. Select for statement and connections.`;
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
    <title id="archify-diagram-title">${esc(data.title)}</title><desc id="archify-diagram-description">Selected main results, important prerequisites, and their recorded connections. This overview does not certify the proof.</desc>
    <defs><marker id="proof-arrow" markerWidth="8" markerHeight="6" refX="7.2" refY="3" orient="auto"><path d="M0 0 L8 3 L0 6 Z" class="proof-arrowhead"/></marker><pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse"><path d="M40 0 L0 0 0 40" class="c-grid" stroke-width="0.5"/></pattern></defs>
    <rect width="100%" height="100%" fill="url(#grid)"/>
    ${graph.components.length > 1 ? graph.components.map((component) => {
      const terminal = component.members.filter((node) => !graph.outgoing.get(node.id).length);
      const ends = (terminal.length ? terminal : component.members.filter((node) => cycleItems.has(node.id))).map((node) => node.label);
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
    'geometry/route-clipping': 'A recorded use extends beyond the SVG viewBox.',
    'geometry/route-not-orthogonal': 'A recorded use has an unexpected diagonal route segment.',
    'geometry/route-endpoint': 'A recorded use does not join its recorded source and target boxes.',
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
    const source = graph.nodes.get(use.from), target = graph.nodes.get(use.to);
    const start = points[0], end = points.at(-1);
    if (start[0] !== source.x + source.width || start[1] <= source.y || start[1] >= source.y + source.height
        || end[0] !== target.x || end[1] <= target.y || end[1] >= target.y + target.height) {
      issue('geometry/route-endpoint', { use: use.id }, { start, end });
    }
    for (let i = 1; i < points.length; i += 1) {
      const [x1, y1] = points[i - 1], [x2, y2] = points[i];
      if (![x1, y1, x2, y2].every(Number.isFinite)) {
        issue('geometry/nonfinite-route', { use: use.id, segment: i - 1 }, {});
        continue;
      }
      if (x1 < 0 || x1 > graph.width || x2 < 0 || x2 > graph.width || y1 < 0 || y1 > graph.height || y2 < 0 || y2 > graph.height) {
        issue('geometry/route-clipping', { use: use.id, segment: i - 1 }, { from: points[i - 1], to: points[i] });
      }
      if (x1 !== x2 && y1 !== y2) issue('geometry/route-not-orthogonal', { use: use.id, segment: i - 1 }, {});
      for (const node of nodes) {
        if (node.id === use.from || node.id === use.to) continue;
        const horizontal = y1 === y2 && y1 > node.y && y1 < node.y + node.height && Math.max(x1, x2) > node.x && Math.min(x1, x2) < node.x + node.width;
        const vertical = x1 === x2 && x1 > node.x && x1 < node.x + node.width && Math.max(y1, y2) > node.y && Math.min(y1, y2) < node.y + node.height;
        if (horizontal || vertical) issue('geometry/route-through-node', { use: use.id, item: node.id, segment: i - 1 }, { from: points[i - 1], to: points[i] });
      }
    }
  }
  const checks = ['finite_node_geometry', 'node_overlaps', 'node_clipping', 'finite_route_geometry', 'route_clipping', 'orthogonal_routes', 'recorded_route_endpoints', 'routes_through_unrelated_nodes'];
  return { status: diagnostics.length ? 'fail' : 'pass', checks, diagnostics, limits: 'Computed node boxes and orthogonal route segments only; text, badge placement, rounded corners, browser behavior, and perceptual review are not checked.' };
}

function representationReceipt(data, html, graphMode) {
  const attributes = (tag) => Object.fromEntries([...tag.matchAll(/([\w:-]+)="([^"]*)"/g)].map((match) => [match[1], match[2]]));
  const nodes = [], uses = [];
  // Inspect the emitted drawing or index, not JSON, templates, or claimed counts.
  const representation = graphMode !== 'index' ? html.match(/<svg\b[\s\S]*?<\/svg>/)?.[0] || '' : html.match(/<details\b[^>]*id="proof-full-index"[\s\S]*?<\/main>/)?.[0] || '';
  for (const match of representation.matchAll(graphMode !== 'index' ? /<(?:g|path)\b[^>]*>/g : /<article\b[^>]*>/g)) {
    const attrs = attributes(match[0]);
    if (graphMode !== 'index') {
      if (Object.hasOwn(attrs, 'data-node-id')) nodes.push(attrs['data-node-id']);
      if (Object.hasOwn(attrs, 'data-edge-id')) uses.push({ id: attrs['data-edge-id'], from: attrs['data-edge-from'], to: attrs['data-edge-to'] });
    } else {
      if (Object.hasOwn(attrs, 'data-proof-index-item')) nodes.push(attrs['data-proof-index-item']);
      if (Object.hasOwn(attrs, 'data-proof-index-use')) uses.push({ id: attrs['data-proof-index-use'], from: attrs['data-proof-from'], to: attrs['data-proof-to'] });
    }
  }
  // Detail rows and detail edges live inside their owner's static index
  // article; the interactive panel duplicates them inside inert templates.
  const stripped = html.replace(/<template\b[\s\S]*?<\/template>/g, '');
  const articles = {};
  for (const match of stripped.matchAll(/<article\b[^>]*id="proof-index-item-[^"]*"[\s\S]*?<\/article>/g)) {
    articles[attributes(match[0].match(/<article\b[^>]*>/)[0])['data-proof-index-item']] = match[0];
  }
  const details = [], detailUses = [];
  for (const match of stripped.matchAll(/<[a-z]+\b[^>]*>/g)) {
    const attrs = attributes(match[0]);
    if (Object.hasOwn(attrs, 'data-proof-detail')) details.push(attrs['data-proof-detail']);
    if (Object.hasOwn(attrs, 'data-proof-detail-use')) detailUses.push({ id: attrs['data-proof-detail-use'], from: attrs['data-proof-from'], to: attrs['data-proof-to'] });
  }
  const detailRows = new Map((data.details || []).map((row) => [row.id, row]));
  // Mirror of the rendering rule: an edge is annotated at its detail "from"
  // endpoint, or at its detail "to" endpoint when only that end is a detail.
  const detailUseOwner = (use) => (detailRows.has(use.from) ? detailRows.get(use.from) : detailRows.get(use.to) || {}).owner;
  const contained = (rows, marker, ownerOf) => rows.every((row) => (articles[ownerOf(row)] || '').includes(`${marker}="${row.id}"`));
  const expectedNodes = data.items.map(({ id }) => id).sort();
  const expectedDetails = (data.details || []).map(({ id }) => id).sort();
  const normalizedUses = (records) => records.map(({ id, from, to }) => ({ id, from, to })).sort((a, b) => a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
  const status = JSON.stringify(nodes.sort()) === JSON.stringify(expectedNodes)
    && JSON.stringify(normalizedUses(uses)) === JSON.stringify(normalizedUses(data.uses))
    && JSON.stringify(details.slice().sort()) === JSON.stringify(expectedDetails)
    && JSON.stringify(normalizedUses(detailUses)) === JSON.stringify(normalizedUses(data.detail_uses || []))
    && contained(data.details || [], 'data-proof-detail', (row) => row.owner)
    && contained(data.detail_uses || [], 'data-proof-detail-use', detailUseOwner)
    ? 'pass' : 'fail';
  return { status, expected_items: data.items.length, rendered_items: nodes.length, expected_uses: data.uses.length, rendered_uses: uses.length, expected_details: expectedDetails.length, rendered_details: details.length, expected_detail_uses: (data.detail_uses || []).length, rendered_detail_uses: detailUses.length, representation: graphMode !== 'index' ? 'svg' : 'index', item_ids: nodes, uses: normalizedUses(uses), detail_ids: details, detail_uses: normalizedUses(detailUses) };
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
    const extractionNote = passage.source_media_type === 'application/pdf' && passage.source_excerpt
      ? '<p class="proof-hint">Approximate text extracted from the PDF. Check the original page for formulas and layout.</p>' : '';
    return `<details><summary>${esc(role)} passage${passage.source_display ? `: ${esc(passage.source_display)}` : ''}</summary>${sourceHtml(passage)}${status ? `<p class="proof-hint">${esc(status)}${method ? ` (${esc(method)})` : ''}. This does not assess the mathematics.</p>` : ''}${verification.note ? `<p class="proof-hint">${esc(verification.note)}</p>` : ''}${extractionNote}${passage.source_excerpt ? `<pre class="proof-excerpt">${esc(passage.source_excerpt)}</pre>` : '<p class="proof-hint">No source excerpt is available for this locator.</p>'}</details>`;
  }).join('')}</div>`;
}

// Fidelity badges report source-comparison bookkeeping, never mathematical
// correctness: one neutral muted style, no sentiment colors.
function fidelityBadgeHtml(fidelity) {
  const state = fidelity || 'unreviewed';
  const labels = {
    unreviewed: 'Not yet compared with the source',
    matched: 'Compared with the source',
    needs_attention: 'Reviewed; source question unresolved',
    stale: 'Changed since the last comparison',
  };
  return `<span class="proof-fidelity" data-proof-fidelity="${esc(state)}">${esc(labels[state] || state)}</span>`;
}

function reviewLineHtml(row) {
  return `<p class="proof-review">${fidelityBadgeHtml(row.fidelity)}<span class="proof-validity-note">Mathematical validity is not assessed by this overview.</span></p>`;
}

function groupBadgeHtml(group) {
  return `<span class="proof-group">${esc(group.kind === 'joint' ? 'Joint' : 'Case')}: ${esc(group.id)}</span>`;
}

function issueHtml(row) {
  return row.issue ? `<div class="proof-uncertainty">Open issue: ${formulaHtml(row.issue_html || esc(row.issue))}</div>` : '';
}

function relationHtml(use, graph, incoming) {
  const other = graph.nodes.get(incoming ? use.from : use.to);
  return `<li><button type="button" data-proof-focus="${esc(other.id)}">${esc(other.label)}</button> ${qualificationHtml(use)}<div class="proof-contribution">${formulaHtml(use.reason_html || esc(use.reason))}</div>${issueHtml(use)}${fidelityBadgeHtml(use.fidelity)}</li>`;
}

function connectionEvidenceHtml(use, graph) {
  const label = `${graph.nodes.get(use.from).label} → ${graph.nodes.get(use.to).label}`;
  return `<details class="proof-use-evidence" data-proof-evidence-use="${esc(use.id)}"><summary>${esc(label)} (${esc(useTypes[use.type])})${use.regime ? `: ${formulaHtml(use.regime_html || esc(use.regime))}` : ''}</summary>${sourceHtml(use)}${passagesHtml(use)}</details>`;
}

function proofIdeaHtml(node) {
  return node.proof_idea ? `<div class="proof-idea">${formulaHtml(node.proof_idea_html || esc(node.proof_idea))}</div>` : '';
}

function qualificationHtml(use) {
  return `<span class="proof-use-type">${esc(useTypes[use.type])}</span>${use.regime ? `<span class="proof-regime">Only in regime: ${formulaHtml(use.regime_html || esc(use.regime))}</span>` : ''}${use.group ? groupBadgeHtml(use.group) : ''}`;
}

// A detail edge is annotated under its detail endpoint's sub-section: the
// "from" row when it is intermediate, otherwise the "to" row. Validation
// guarantees at least one intermediate endpoint, so each edge lands in
// exactly one owner's panel.
function detailAnchor(use, graph) {
  return graph.details.has(use.from) ? use.from : use.to;
}

function detailUseHtml(use, graph) {
  const label = (id) => (graph.nodes.get(id) || graph.details.get(id) || {}).label || id;
  return `<div class="proof-detail-use" data-proof-detail-use="${esc(use.id)}" data-proof-from="${esc(use.from)}" data-proof-to="${esc(use.to)}">${qualificationHtml(use)} ${esc(label(use.from))} → ${esc(label(use.to))}<div>${formulaHtml(use.reason_html || esc(use.reason))}</div>${issueHtml(use)}${fidelityBadgeHtml(use.fidelity)}${use.source_display ? `<small>${esc(use.source_display)}</small>` : ''}${passagesHtml(use)}<p class="proof-hint">This use refines the recorded argument at an intermediate step; it is not part of the overview graph.</p></div>`;
}

function detailSectionHtml(detail, graph) {
  const annotations = graph.detailUses.filter((use) => detailAnchor(use, graph) === detail.id);
  return `<section class="proof-detail" data-proof-detail="${esc(detail.id)}"><h5><span class="proof-detail-kind">${esc(detailKinds[detail.kind])}</span> ${esc(detail.label)}${detail.caption ? `: ${esc(detail.caption)}` : ''}</h5><div class="proof-statement">${formulaHtml(detail.statement_html)}</div>${detail.proof_idea ? `<section class="proof-reading-section proof-logic-section"><h5>Proof idea</h5>${proofIdeaHtml(detail)}</section>` : ''}${issueHtml(detail)}${fidelityBadgeHtml(detail.fidelity)}${passagesHtml(detail)}${annotations.map((use) => detailUseHtml(use, graph)).join('')}</section>`;
}

function fullItemHtml(node, graph, { hover = false } = {}) {
  const incoming = graph.incoming.get(node.id), outgoing = graph.outgoing.get(node.id);
  if (hover) return `<strong>${esc(node.label)}</strong><p class="proof-hover-caption">${esc(node.caption || kinds[node.kind][0])}</p>${sourceHtml(node)}<p class="proof-hint">Click or press Enter for the statement and connections.</p>`;
  const details = graph.detailsByOwner.get(node.id) || [];
  const connections = [...new Map([...incoming, ...outgoing].map((use) => [use.id, use])).values()];
  return `
    <section class="proof-reading-section proof-statement-section"><h4>${node.statement_form === 'synopsis' ? 'Statement synopsis' : 'Statement'}</h4><div class="proof-statement">${formulaHtml(node.statement_html)}</div></section>
      <section class="proof-reading-section proof-logic-section proof-dependencies">${node.proof_idea ? `<h4>Proof idea</h4>${proofIdeaHtml(node)}<h5>How the inputs contribute (${incoming.length})</h5>` : `<h4>How the inputs contribute (${incoming.length})</h4>`}${incoming.length ? `<ul>${incoming.map((use) => relationHtml(use, graph, true)).join('')}</ul><p class="proof-hint">These are recorded inputs to the argument. Their joint sufficiency has not been verified by this overview.</p>` : '<p>No prerequisite use is recorded in this overview.</p>'}</section>
      ${issueHtml(node)}${reviewLineHtml(node)}
      <div class="proof-dependencies proof-downstream"><h4>Used by (${outgoing.length})</h4>${outgoing.length ? `<ul>${outgoing.map((use) => relationHtml(use, graph, false)).join('')}</ul>` : '<p>No downstream use is recorded in this overview.</p>'}</div>
      <details class="proof-evidence"><summary>Sources and locators</summary>${sourceHtml(node)}${Array.isArray(node.aliases) && node.aliases.length ? `<p class="proof-hint">Also identified as: ${node.aliases.map((alias) => esc(alias)).join(', ')}</p>` : ''}${passagesHtml(node)}${connections.length ? `<h5>Connection evidence</h5>${connections.map((use) => connectionEvidenceHtml(use, graph)).join('')}` : ''}</details>
      ${details.length ? `<div class="proof-details"><h4>Intermediate steps (${details.length})</h4>${details.map((detail) => detailSectionHtml(detail, graph)).join('')}</div>` : ''}
      `.trim();
}

function fullUseHtml(use, graph) {
  return `<h4>${esc(graph.nodes.get(use.from).label)} → ${esc(graph.nodes.get(use.to).label)}</h4>${qualificationHtml(use)}<section class="proof-reading-section proof-logic-section"><h4>How this input contributes</h4><div class="proof-idea">${formulaHtml(use.reason_html || esc(use.reason))}</div></section>${issueHtml(use)}${reviewLineHtml(use)}<details class="proof-evidence"><summary>Sources and locators</summary>${sourceHtml(use)}${passagesHtml(use)}</details><p class="proof-hint">This connection records a use in the argument. This overview does not verify that inference.</p>`;
}

function fullIndexHtml(data, graph, open = false) {
  const detailCount = (data.details || []).length;
  const detailSummary = detailCount ? `, ${detailCount} intermediate steps and ${data.detail_uses.length} detail uses under their owners` : '';
  return `<details class="proof-index" id="proof-full-index"${open ? ' open' : ''}><summary>Selected statements and connections (${data.items.length} statements, ${data.uses.length} connections${detailSummary})</summary>${data.items.map((node) => `<article id="proof-index-item-${esc(node.id)}" data-proof-index-item="${esc(node.id)}"><h3>${esc(node.label)}${node.caption ? `: ${esc(node.caption)}` : ''}</h3>${fullItemHtml(node, graph)}</article>`).join('')}<h3>Connections</h3>${data.uses.map((use) => `<article id="proof-index-use-${esc(use.id)}" data-proof-index-use="${esc(use.id)}" data-proof-from="${esc(use.from)}" data-proof-to="${esc(use.to)}">${fullUseHtml(use, graph)}</article>`).join('')}</details>`;
}

function recordsHtml(data, graphMode) {
  const records = { schema_version: data.schema_version, graph_mode: graphMode, ...(data.graph_cycles !== undefined ? { graph_cycles: data.graph_cycles } : {}), items: data.items, uses: data.uses, details: data.details, detail_uses: data.detail_uses, build_context: data.build_context || { mathematical_assessment: 'not_performed' } };
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
  const comparison = context.source_comparison?.summary || { complete: 'Source comparisons are complete.', incomplete: 'Source comparisons have unfinished work or unresolved questions; see individual records.' }[comparisonStatus] || '';
  const candidates = context.citation_candidates;
  const scan = typeof candidates === 'string'
    ? 'Citation scan not applicable to the captured sources.'
    : (candidates && typeof candidates.attributed === 'number'
      ? `Citation scan: ${typeof candidates.pairs === 'number' ? `${candidates.pairs} cited result pairs; ` : ''}${candidates.attributed} attributed and ${candidates.unattributed} unattributed matched references; ${typeof candidates.not_mechanically_matchable === 'number' ? `${candidates.not_mechanically_matchable} records without unique citation labels; ` : ''}${candidates.missing_uses} missing-use candidates; ${candidates.unsupported_uses} recorded uses not corroborated by this scan.`
      : '');
  const rows = [...(data.items || []), ...(data.details || []), ...(data.uses || []), ...(data.detail_uses || [])];
  const issues = rows.filter((row) => typeof row.issue === 'string' && row.issue.trim()).length;
  const issueNote = issues ? `${issues} record${issues === 1 ? ' carries' : 's carry'} an open issue note.` : '';
  return `<p class="proof-hint">${revision ? `<span title="Captured revision ${esc(revision.slice(0, 12))}">Captured manuscript version.</span> ` : ''}${sourceStatus ? `${sourceStatus} ` : ''}${comparison ? `${esc(comparison)} ` : ''}${scan ? `${scan} ` : ''}${issueNote ? `${issueNote} ` : ''}Mathematical assessment has not been performed.</p>`;
}

function scopeHtml(scope, prepared) {
  if (prepared) {
    const lead = prepared.lead_html ? `<p class="proof-scope-lead">${formulaHtml(prepared.lead_html)}</p>` : '';
    const details = prepared.details_html ? `<details class="proof-scope"><summary>Scope details and reading limits</summary><p>${formulaHtml(prepared.details_html)}</p></details>` : '';
    return lead + details;
  }
  // Bound presentation of older, unbroken scope essays without rewriting the
  // stored scope. The complete original remains available in one disclosure.
  const original = String(scope || '').trim();
  const [lead, ...rest] = original.split(/\n\s*\n/);
  const words = [...lead.matchAll(/\S+/gu)];
  const abbreviated = words.length > 100;
  const preview = abbreviated ? `${lead.slice(0, words[100].index).trimEnd()}…` : lead.trim();
  const remainder = abbreviated ? original : rest.join('\n\n').trim();
  const leadHtml = preview ? `<p class="proof-scope-lead">${esc(preview)}</p>` : '';
  const restHtml = remainder ? `<details class="proof-scope"><summary>Scope details and reading limits</summary><p>${esc(remainder)}</p></details>` : '';
  return leadHtml + restHtml;
}

function mathDiagnosticsHtml(data) {
  const entries = Array.isArray(data.math_diagnostics) ? data.math_diagnostics : [];
  if (!entries.length) return '';
  const groups = new Map();
  for (const entry of entries) {
    if (!groups.has(entry.reason)) groups.set(entry.reason, []);
    groups.get(entry.reason).push(entry);
  }
  const itemNames = new Map([...(data.items || []), ...(data.details || [])].map((item) => [item.id, item.label]));
  const useNames = new Map();
  for (const use of [...(data.uses || []), ...(data.detail_uses || [])]) {
    useNames.set(use.id, `${itemNames.get(use.from) || use.from} → ${itemNames.get(use.to) || use.to}`);
  }
  const rows = [...groups].map(([reason, occurrences]) => `<li><details><summary>${esc(reason)} (${occurrences.length})</summary><ul>${occurrences.map((entry) => `<li>${esc((entry.collection === 'uses' ? useNames : itemNames).get(entry.id) || entry.id)} (${esc(entry.field)}): <code>${esc(entry.id)}</code><br><code>${esc(entry.excerpt)}</code></li>`).join('')}</ul></details></li>`).join('');
  return `<details class="proof-render-warnings"><summary>Math display notes: ${entries.length} expression(s) kept as labeled LaTeX</summary><ul>${rows}</ul></details>`;
}

function cycleContextHtml(graph) {
  if (!graph.cycles.length) return '';
  const uses = new Map([...graph.outgoing.values()].flat().map((use) => [use.id, use]));
  return `<details class="proof-render-warnings proof-cycles"><summary>Recorded cycles (${graph.cycles.length}): all connections remain visible</summary><p>Return arrows preserve the recorded direction. A cycle can reflect argument reuse, alternative regimes, or an unresolved dependency; it does not establish a circular proof. Read the connections and their source passages before interpreting it.</p><ul>${graph.cycles.map((group) => `<li>${group.item_ids.map((id) => `<button type="button" data-proof-focus="${esc(id)}">${esc(graph.nodes.get(id).label)}</button>`).join(', ')}<ul>${group.use_ids.map((id) => {
    const use = uses.get(id);
    return `<li><button type="button" data-proof-use="${esc(id)}">${esc(graph.nodes.get(use.from).label)} → ${esc(graph.nodes.get(use.to).label)} (${esc(useTypes[use.type])})</button>${use.regime ? `; ${esc(use.regime)}` : ''}</li>`;
  }).join('')}</ul></li>`).join('')}</ul></details>`;
}

function renderIndex(data) {
  const graph = indexGraph(data);
  const warnings = (data.warnings || []).map((warning) => `<li>${esc(warning)}</li>`).join('');
  const html = `<!doctype html><html lang="en" data-theme="light"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${esc(data.title)} | Proof overview index</title>${proofCss(data)}<style>
    :root{--text:#172033;--text-muted:#526075;--text-dim:#637086;--panel:#f8fafc;--panel-border:#d7dee8;--mask:#fff;--arrow:#64748b;color-scheme:light}html[data-theme="dark"]{--text:#e5eaf3;--text-muted:#adb8cb;--text-dim:#9caac0;--panel:#101827;--panel-border:#344155;--mask:#172132;color-scheme:dark}body{margin:0;background:var(--panel);color:var(--text);font:14px/1.6 system-ui,sans-serif}main{max-width:1000px;margin:auto;padding:28px 24px}h1{font-size:22px;line-height:1.3}.proof-index article{scroll-margin-top:18px}.proof-passages details{margin:8px 0}.proof-passages summary{cursor:pointer;font-size:12px;overflow-wrap:anywhere}.proof-index h4{font-size:13px}.proof-index-controls{display:flex;flex-wrap:wrap;gap:8px}.proof-index-controls a,.proof-index-controls button{font:inherit;color:var(--text);border:1px solid var(--panel-border);border-radius:5px;background:var(--mask);padding:5px 9px;text-decoration:none}@media print{main{max-width:none;padding:0}}
    </style></head><body><main><h1>${esc(data.title)}</h1><p class="proof-caption">${data.items.length} selected statements · ${data.uses.length} connections</p><p><strong>Index view.</strong> All selected statements and connections are displayed below. Cyclic mappings alone do not establish a circular proof.</p>${scopeHtml(data.scope, data.scope_display)}${buildContextHtml(data)}${warnings ? `<div class="proof-render-warnings"><ul>${warnings}</ul></div>` : ''}${mathDiagnosticsHtml(data)}<nav class="proof-index-controls" aria-label="Index controls"><button type="button" id="proof-index-theme">Switch theme</button><a href="#proof-full-index">Selected statements and connections</a></nav>${fullIndexHtml(data, graph, true)}</main>${recordsHtml(data, 'index')}<script>(function(){document.getElementById('proof-index-theme').addEventListener('click',function(){document.documentElement.setAttribute('data-theme',document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark');});document.addEventListener('click',function(event){var button=event.target.closest('[data-proof-focus]');if(!button)return;var article=document.getElementById('proof-index-item-'+button.getAttribute('data-proof-focus'));if(article){document.getElementById('proof-full-index').open=true;article.scrollIntoView({block:'start'});article.setAttribute('tabindex','-1');article.focus({preventScroll:true});}});})();</script></body></html>`;
  const graph_preservation = representationReceipt(data, html, 'index');
  if (graph_preservation.status !== 'pass') throw new Error('Rendered index does not preserve the supplied item and use identities.');
  return { html, items: data.items.length, uses: data.uses.length, viewBox: null, graph_mode: 'index', graph_preservation, geometry: { status: 'not_applicable', checks: [], diagnostics: [], reason: 'The complete records are shown as an index; no dependency geometry is drawn.' } };
}

function proofCss(data) {
  const badgeFocus = data.uses.filter((use) => use.type !== 'dependency' || use.regime || use.group).map((use) => `svg[data-focus-active]:not([data-reach-active]):has([data-edge-id="${use.id}"][data-focus-match]) [data-proof-use="${use.id}"],svg[data-reach-active]:has([data-edge-id="${use.id}"][data-reach-match]) [data-proof-use="${use.id}"]{opacity:1}`).join('');
  const palette = Object.entries(kinds).map(([kind, [, dark, light]]) => `
    [data-theme="dark"] .c-${kind}{--proof-tone:${dark}} [data-theme="light"] .c-${kind}{--proof-tone:${light}}
    .c-${kind}{stroke:var(--proof-tone,${dark});fill:color-mix(in srgb,var(--proof-tone,${dark}) 13%,transparent)}
    [data-theme="dark"] [data-kind="${kind}"]{--proof-kind:${dark}} [data-theme="light"] [data-kind="${kind}"]{--proof-kind:${light}}
    .overview-map-node[data-kind="${kind}"]{fill:var(--proof-kind,${dark});stroke:var(--proof-kind,${dark})}
    .semantic-lens-kind[data-kind="${kind}"]{--lens-color:var(--proof-kind,${dark})}
    .proof-legend [data-kind="${kind}"]::before{background:var(--proof-kind,${dark})}
  `.trim()).join('\n');
  return `<style id="proof-overview-style">
    :root{--proof-reading-text:#172033;--proof-reading-muted:#475569;--proof-statement-bg:#eff6ff;--proof-statement-accent:#3b82f6;--proof-statement-label:#1e40af;--proof-logic-bg:#f5f3ff;--proof-logic-accent:#8b5cf6;--proof-logic-label:#5b21b6}
    [data-theme="dark"]{--proof-reading-text:#e5eaf3;--proof-reading-muted:#b6c5d9;--proof-statement-bg:#142238;--proof-statement-accent:#60a5fa;--proof-statement-label:#bfdbfe;--proof-logic-bg:#241d36;--proof-logic-accent:#a78bfa;--proof-logic-label:#ddd6fe}
    ${palette}
    svg[data-focus-active] .proof-edge-badge{opacity:.13}svg[data-reach-active] .proof-edge-badge{opacity:.09}${badgeFocus}
    .proof-edge{fill:none;stroke:var(--arrow)} .proof-uncertain{stroke-dasharray:6 4}.proof-arrowhead{fill:var(--arrow)}
    .proof-argument-edge{stroke-width:2.2}.proof-edge-badge{cursor:pointer}.proof-edge-badge rect{fill:var(--mask);stroke:var(--panel-border)}.proof-edge-badge text{fill:var(--text-muted);font-size:10px}.proof-edge-badge:focus-visible rect{stroke:var(--text);stroke-width:2}.proof-badge-leader{fill:none;stroke:var(--arrow);stroke-width:1;stroke-dasharray:2 3;pointer-events:none}.proof-badge-anchor{fill:var(--arrow);pointer-events:none}.proof-component path{stroke:var(--panel-border);stroke-width:1}.proof-component text{fill:var(--text-muted);font-size:12px;paint-order:stroke;stroke:var(--panel);stroke-width:6px}
    .proof-use-type,.proof-regime{display:inline-block;font-size:10px;border:1px solid var(--panel-border);padding:2px 6px;border-radius:4px;margin:2px 3px;color:var(--text-muted)}
    .proof-group,.proof-detail-kind,.proof-fidelity{display:inline-block;font-size:10px;border:1px solid var(--panel-border);padding:2px 6px;border-radius:4px;margin:2px 3px;color:var(--text-muted)}
    .proof-details{margin-top:10px}.proof-detail{border-top:1px dashed var(--panel-border);padding-top:6px;margin-top:8px}.proof-detail h5{font-size:12px;color:var(--text);margin:6px 0}
    .proof-detail-use{margin:8px 0;padding:4px 8px;border-left:2px solid var(--panel-border);font-size:11px;line-height:1.6;color:var(--text-muted)}
    .proof-detail-use small{display:block;color:var(--text-dim)}
    .proof-review{margin:8px 0}.proof-validity-note{font-size:11px;color:var(--text-muted);margin-left:6px}
    .proof-navigation{margin:12px 0 14px}.proof-navigation h2{font-size:13px;color:var(--text);margin:0 0 8px}.proof-main-list{display:flex;gap:8px;overflow-x:auto;padding-bottom:4px;scrollbar-width:thin}.proof-main-list button{flex:0 0 auto;text-align:left;max-width:240px;padding:8px 12px;border-radius:6px;border:1px solid var(--panel-border);border-left:3px solid var(--proof-tone);background:var(--panel);color:var(--text);font:inherit;font-size:12px;cursor:pointer}.proof-main-list button strong{display:block;font-size:13px}.proof-main-list button span{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:11px;color:var(--text-muted);margin-top:4px}.proof-main-list button[aria-pressed="true"]{outline:1px solid var(--proof-tone)}
    .proof-view-controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0}.proof-view-controls button{background:var(--panel);border:1px solid var(--panel-border);border-radius:5px;padding:6px 10px;color:var(--text);font:inherit;font-size:11px;cursor:pointer}.proof-view-controls button:focus-visible,.proof-main-list button:focus-visible{outline:2px solid var(--text);outline-offset:2px}.proof-view-controls p{font-size:11px;color:var(--text-muted);margin:0}
    .proof-scope{font-size:12px;line-height:1.6;color:var(--text-muted);margin:8px 0}.proof-scope summary{cursor:pointer}.proof-scope p{max-width:none}.proof-scope-lead{font-size:13px;line-height:1.6;margin:8px 0;max-width:76em}.proof-trace-note{font-size:11px;color:var(--text-muted);line-height:1.5;margin:8px 0}.diagram-container>svg{max-height:620px}.diagram-container{scroll-margin-top:80px}
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
    .proof-reading-section{--text:var(--proof-reading-text);--text-muted:var(--proof-reading-muted);--text-dim:var(--proof-reading-muted);color:var(--text);border:1px solid var(--panel-border);border-left:3px solid var(--proof-section-accent);border-radius:6px;padding:9px 12px;margin:10px 0;min-width:0}
    .proof-statement-section{--proof-section-accent:var(--proof-statement-accent);--proof-section-label:var(--proof-statement-label);background:var(--proof-statement-bg)}
    .proof-logic-section{--proof-section-accent:var(--proof-logic-accent);--proof-section-label:var(--proof-logic-label);background:var(--proof-logic-bg)}
    .proof-reading-section h4{margin:0 0 5px;font-size:12px;line-height:1.5;color:var(--proof-section-label)}.proof-reading-section h5{margin:10px 0 5px;font-size:11px;line-height:1.5;color:var(--proof-section-label)}
    .proof-reading-section .proof-statement{margin:0}.proof-idea{font-family:Georgia,'Times New Roman',serif;font-size:15px;line-height:1.55;overflow-wrap:anywhere;white-space:pre-line}.proof-logic-section li{font-size:12px;color:var(--text)}.proof-contribution{margin:3px 0;overflow-wrap:anywhere}.proof-logic-section>.proof-hint{margin:7px 0 0}
    .proof-downstream{padding-top:0}.proof-evidence{border-top:1px solid var(--panel-border);margin:10px 0;padding-top:8px}.proof-evidence summary{cursor:pointer;font-size:11px;color:var(--text-muted);overflow-wrap:anywhere}.proof-evidence summary:focus-visible{outline:2px solid var(--text);outline-offset:3px}.proof-evidence h5{font-size:11px;color:var(--text);margin:10px 0 5px}.proof-use-evidence{margin:8px 0}.proof-use-evidence>summary{line-height:1.6}
    [data-proof-focus]{border:0;background:transparent;color:var(--text);text-decoration:underline;font:inherit;cursor:pointer;padding:0}
    .proof-excerpt{font-size:11px;white-space:pre-wrap;overflow-wrap:anywhere;color:var(--text-muted);line-height:1.5}
    .proof-passages details{margin:8px 0}.proof-passages summary{cursor:pointer;font-size:11px;overflow-wrap:anywhere}.proof-statement-form{margin:8px 0 -4px;font-weight:600}
    #proof-tooltip{position:fixed;z-index:10000;width:320px;max-width:calc(100vw - 24px);box-sizing:border-box;padding:12px 15px;border:1px solid var(--panel-border);border-radius:8px;background:var(--mask);box-shadow:0 12px 35px #0003;pointer-events:none;color:var(--text);font-size:12px}#proof-tooltip[hidden]{display:none}.proof-hover-caption{margin:6px 0;line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}#proof-tooltip .proof-source{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}#proof-tooltip .proof-hint{margin:7px 0 0}
    .proof-index{margin:14px 0 0;color:var(--text-muted);font-size:12px}.proof-index>summary{cursor:pointer}.proof-index article{padding:16px 0;border-top:1px solid var(--panel-border);break-inside:avoid}.proof-index h3{font-size:14px;color:var(--text);margin:5px 0}
    .proof-attribution{font-size:10px;color:var(--text-dim);margin:15px 0 0}
    .proof-render-warnings{font-size:12px;line-height:1.6;color:var(--text);border:1px solid var(--panel-border);border-radius:8px;padding:10px 14px;margin:12px 0}.proof-render-warnings ul{padding-left:20px;margin:5px 0}.math-fallback{font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere;border-bottom:1px dotted var(--text-muted)}
    @media(max-width:720px){.proof-main-list button{max-width:200px}.proof-view-controls p{width:100%}.diagram-container>svg{max-height:560px}}
    @media print{#proof-tooltip,#focus-chip,.proof-view-controls{display:none!important}.diagram-container>svg{max-height:none}.proof-index>summary{display:none}.proof-index article{display:block}.proof-reading-section{--proof-reading-text:#172033;--proof-reading-muted:#475569;--proof-statement-bg:#eff6ff;--proof-statement-label:#1e40af;--proof-logic-bg:#f5f3ff;--proof-logic-label:#5b21b6}.proof-index .proof-statement{color:#111}.proof-index details[open]>summary{display:none}}
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
    var panel=document.createElement('div');panel.id='proof-selection';panel.setAttribute('aria-label','Selected statement and connections');
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
      status.textContent='Readable view shows part of the selected graph. Drag the background or select a result to navigate.';
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
  const graphMode = graph.cycles.length ? 'cyclic' : 'dag';
  if (data.graph_mode !== undefined && data.graph_mode !== graphMode) throw new Error('Prepared graph_mode does not match the recorded connections. Prepare the records again.');
  if (data.graph_cycles !== undefined && JSON.stringify(data.graph_cycles) !== JSON.stringify(graph.cycles)) throw new Error('Prepared cycle diagnostics do not match the recorded connections. Prepare the records again.');
  // A terminal cyclic group has no individual sink. Keep its members available
  // as reading entry points without presenting their internal order as a proof.
  const terminalCycles = graph.cycles.filter((group) => {
    const members = new Set(group.item_ids);
    return group.item_ids.every((id) => graph.outgoing.get(id).every((use) => members.has(use.to)));
  });
  const terminalIds = new Set([...data.items.filter((item) => !graph.outgoing.get(item.id).length).map((item) => item.id), ...terminalCycles.flatMap((group) => group.item_ids)]);
  const mainItems = data.main_items || data.items.filter((item) => terminalIds.has(item.id)).map((item) => item.id);
  const hasRegimes = data.uses.some((use) => use.regime);
  const presentKinds = new Set(data.items.map((item) => item.kind));
  const legend = `<ul class="proof-legend" aria-label="Mathematical item types">${Object.entries(kinds).filter(([kind]) => presentKinds.has(kind)).map(([kind, [label]]) => `<li data-kind="${kind}">${label}</li>`).join('')}</ul>`;
  const cards = `<div class="proof-caption"><p>Arrows point from prerequisites to the results that use them. Badges distinguish definition uses, proof arguments, and regime-specific uses. Dashed arrows mark connections with an open issue.</p></div>${legend}`;
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
  // Proof statements and disclosures intentionally extend the document. The
  // upstream one-screen fitter alternates between widening the graph and
  // shrinking it for document overflow, retriggering its own ResizeObserver.
  // Keep the existing reader API and mode handling, but size from width only.
  template = replaceTemplateOnce(template,
    'var desiredWidth = availableSvgHeight * ratio + chrome.diagramX;',
    'var desiredWidth = maxWidth;');
  template = replaceTemplateOnce(template,
    'settleOverflow(minWidth);',
    '// Expanded proof content uses document scrolling without resizing the reader.');
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
  const navigation = `<section class="proof-navigation" aria-labelledby="proof-main-title"><h2 id="proof-main-title">${data.main_items ? 'Main results' : terminalCycles.length ? 'Results and terminal cycle groups' : 'Terminal results in the recorded graph'}</h2><div class="proof-main-list" id="proof-main-results">${mainItems.map((id) => {
    const node = graph.nodes.get(id);
    return `<button type="button" data-proof-main="${esc(id)}" class="c-${esc(node.kind)}" aria-pressed="false" title="${esc(`${node.label}: ${node.caption}. Open statement and supporting dependencies.`)}"><strong>${esc(node.label)}</strong><span>${esc(node.caption)}</span></button>`;
  }).join('')}</div><div class="proof-view-controls"><button type="button" id="proof-full-structure">Fit selected graph (${data.items.length} statements)</button><button type="button" id="proof-readable-view">Readable view</button><p id="proof-view-status" aria-live="polite">Select a result to read its statement and explore its prerequisites.</p></div>${hasRegimes ? '<p class="proof-trace-note" role="note">Some connections apply only in a named regime. Dependency tracing follows all recorded arrows, including alternative routes. Regime labels still apply; a trace is not one required or verified proof route.</p>' : ''}</section>`;
  html = html.replace('<div class="diagram-container"', () => `<div class="proof-caption"><p>${esc(data.source?.title || data.title)} · ${data.items.length} selected statements · ${data.uses.length} connections</p><p><strong>Selected main results and important prerequisites.</strong> Select a result for its statement, source, and connections. This map explains the written argument; it does not verify the proofs.</p>${buildContextHtml(data)}</div>${scopeHtml(data.scope, data.scope_display)}${warnings}${mathDiagnosticsHtml(data)}${cycleContextHtml(graph)}${navigation}\n<div class="diagram-container"`);
  const fragments = data.items.map((node) => `<template id="proof-detail-${esc(node.id)}">${fullItemHtml(node, graph)}</template><template id="proof-hover-${esc(node.id)}">${fullItemHtml(node, graph, { hover: true })}</template>`).join('');
  const useFragments = data.uses.map((use) => `<template id="proof-use-${esc(use.id)}">${fullUseHtml(use, graph)}</template>`).join('');
  const index = `${fullIndexHtml(data, graph)}<p class="proof-attribution">Viewer adapted from Archify 2.17 by tt-a1i and Cocoon AI, MIT licensed. Proof-specific dataset and rendering by proof-graphify.</p>`;
  // applyTemplate replaces the complete cards slot including its sentinels.
  html = html.replace(cards, () => `${cards}${index}`);
  html = html.replace('</body>', () => `${fragments}${useFragments}${recordsHtml(data, graphMode)}${proofRuntime({ items: [...graph.nodes.values()].map(({ id, x, y }) => ({ id, x: x + box.w / 2, y: y + box.h / 2 })), uses: data.uses.map(({ id }) => ({ id })), main_items: mainItems })}</body>`);
  const graph_preservation = representationReceipt(data, html, graphMode);
  if (graph_preservation.status !== 'pass') throw new Error('Rendered SVG does not preserve the supplied item and use identities.');
  return { html, items: data.items.length, uses: data.uses.length, viewBox: [graph.width, graph.height], graph_mode: graphMode, graph_preservation, geometry };
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
