#!/usr/bin/env node
// render_projection.mjs - self-contained HTML renderer for proof-audit projections.
//
//   node render_projection.mjs INPUT.json OUTPUT.html
//
// Writes one self-contained HTML file (inline CSS/JS, embedded fonts, no
// external requests), prints one JSON receipt to stdout and exits 0. On any
// failure it prints a JSON diagnostic to stderr, exits nonzero and leaves no
// output file behind (the page is written next to OUTPUT and renamed only
// after every verification passed). Output is deterministic: identical input
// bytes produce identical HTML bytes.
//
// The palette, box metrics, longest-path layering, orthogonal edge routing and
// badge placement are adapted from archify-proofs-overview/scripts/render.mjs
// (MIT). See THIRD_PARTY_NOTICES.md.

import { readFileSync, writeFileSync, renameSync, unlinkSync, existsSync } from 'node:fs';
import { createHash } from 'node:crypto';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';
import { esc, textUnits } from './assets/archify/utils.mjs';
import { scanHtml, textOf, descendants, hasAttr, sameMultiset } from './html_scan.mjs';
import { archifyPage, mainNavigation } from './archify_adapter.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const TEMPLATE_PATH = path.join(HERE, 'assets', 'archify', 'template.html');

// ---------------------------------------------------------------------------
// Vocabulary
// ---------------------------------------------------------------------------

// [display name, dark-theme tone, light-theme tone] - the archify palette.
export const KINDS = {
  assumption: ['Assumption', '#60a5fa', '#2563eb'],
  definition: ['Definition', '#94a3b8', '#475569'],
  lemma: ['Lemma', '#c4b5fd', '#7c3aed'],
  proposition: ['Proposition', '#a5b4fc', '#4f46e5'],
  theorem: ['Theorem', '#e879f9', '#a21caf'],
  corollary: ['Corollary', '#67e8f9', '#0e7490'],
  external_result: ['External result', '#cbd5e1', '#64748b'],
};
export const BOX = { w: 170, h: 64, column: 275, row: 108, margin: 36 };
export const STATES = ['green', 'red', 'gray', 'amber'];
const STATE_LABELS = { green: 'Green', red: 'Red', gray: 'Gray', amber: 'Amber' };
const STATE_GLYPHS = { green: '✓', red: '✕', gray: '?', amber: '!' };
const REVIEWS = ['not_required', 'pending', 'complete', 'disputed', 'compromised'];
const REVIEW_LABELS = { not_required: 'not required', pending: 'pending', complete: 'complete', disputed: 'disputed', compromised: 'compromised' };
const SECTION_KINDS = ['statement', 'applications', 'derivations', 'premises', 'coverage', 'findings', 'sources', 'review', 'composition', 'limitations'];
const ROLES = ['primary', 'independent', 'coordinator'];
const BUILD_KINDS = ['working', 'release'];
const LAYOUT_MODES = ['dag', 'index'];
const DISPLAY_KEYS = ['statement_html', 'reason_html', 'needed_form_html', 'rationale_html', 'conditions_html', 'reasoning_html', 'description_html'];
// Record body field -> display fragment that replaces its raw text.
const FRAGMENT_FIELDS = { statement: 'statement_html', reason: 'reason_html', needed_form: 'needed_form_html', rationale: 'rationale_html', conditions: 'conditions_html', reasoning: 'reasoning_html', description: 'description_html' };
const COUNT_NAMES = [
  ...STATES.map((state) => `nodes.${state}`),
  ...STATES.map((state) => `connections.${state}`),
  'findings.open', 'findings.resolved', 'findings.superseded',
  'progress.required_obligations', 'progress.completed_current_obligations', 'progress.draft_checks', 'progress.major_results', 'progress.source_unbound_items',
];
const PROGRESS_FIELDS = ['required_obligations', 'completed_current_obligations', 'draft_checks', 'major_results', 'source_unbound_items'];

const ID_PATTERN = /^[A-Za-z][A-Za-z0-9_.:-]{0,254}$/;
const COLLECTION_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;
const KEY_PATTERN = /^[^\u0000-\u001f\u007f]{1,512}$/u;

// ---------------------------------------------------------------------------
// Errors and small helpers
// ---------------------------------------------------------------------------

export class RenderError extends Error {
  constructor(stage, message, diagnostics = []) {
    super(message);
    this.name = 'RenderError';
    this.stage = stage;
    this.diagnostics = diagnostics;
  }
}

function fail(stage, message, diagnostics = []) {
  throw new RenderError(stage, message, diagnostics);
}

const isObject = (value) => typeof value === 'object' && value !== null && !Array.isArray(value);
const isString = (value) => typeof value === 'string';
const isCount = (value) => Number.isInteger(value) && value >= 0;
const sha256 = (bytes) => createHash('sha256').update(bytes).digest('hex');
const round2 = (value) => Math.round(value * 100) / 100;
const fmt = (value) => String(round2(value));
const refKey = (ref) => `${ref.collection}:${ref.id}:${ref.version}`;
const looseKey = (ref) => `${ref.collection}:${ref.id}`;
const humanize = (key) => String(key).replaceAll('_', ' ');

export function jsonForScript(value) {
  return JSON.stringify(value).replaceAll('<', '\\u003c').replaceAll('>', '\\u003e').replaceAll('&', '\\u0026');
}

// ---------------------------------------------------------------------------
// Input parsing
// ---------------------------------------------------------------------------

// JSON.parse silently keeps the last duplicate key; a projection with two
// different bodies for one key must not be rendered as if it were unambiguous.
function findDuplicateKey(text) {
  const stack = [];
  const length = text.length;
  let i = 0;
  while (i < length) {
    const ch = text[i];
    if (ch === '"') {
      let j = i + 1;
      while (j < length && text[j] !== '"') j += text[j] === '\\' ? 2 : 1;
      const value = JSON.parse(text.slice(i, j + 1));
      const top = stack[stack.length - 1];
      if (top && top.type === 'object' && top.expectKey) {
        if (top.keys.has(value)) {
          const where = stack.map((frame) => (frame.type === 'object' ? frame.current : `[${frame.index}]`)).filter((part) => part !== null).join('.');
          return { key: value, path: where || '(root)' };
        }
        top.keys.add(value);
        top.current = value;
        top.expectKey = false;
      }
      i = j + 1;
      continue;
    }
    if (ch === '{') stack.push({ type: 'object', keys: new Set(), expectKey: true, current: null });
    else if (ch === '[') stack.push({ type: 'array', index: 0 });
    else if (ch === '}' || ch === ']') stack.pop();
    else if (ch === ',') {
      const top = stack[stack.length - 1];
      if (top && top.type === 'object') top.expectKey = true;
      else if (top) top.index += 1;
    }
    i += 1;
  }
  return null;
}

export function parseInput(bytes) {
  let text = Buffer.isBuffer(bytes) ? bytes.toString('utf8') : String(bytes);
  if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
  let value;
  try {
    value = JSON.parse(text);
  } catch (error) {
    fail('parse', `Input is not valid JSON: ${error.message}`);
  }
  if (!isObject(value)) fail('parse', 'Input must be a JSON object.');
  const duplicate = findDuplicateKey(text);
  if (duplicate) fail('parse', `Input contains duplicate key ${JSON.stringify(duplicate.key)} in object ${duplicate.path}.`);
  return value;
}

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

export function validateInput(input) {
  const problems = [];
  const bad = (message) => { if (problems.length < 60) problems.push(message); };
  const done = () => { if (problems.length) fail('validate', `Input rejected: ${problems.length} problem${problems.length === 1 ? '' : 's'}.`, problems); };

  if (input.render_input_version !== 1) bad('render_input_version must be 1');
  if (!isString(input.title) || !input.title.trim()) bad('title must be a nonempty string');
  const build = input.build;
  if (!isObject(build)) bad('build must be an object');
  else {
    if (!isString(build.core_version)) bad('build.core_version must be a string');
    if (!isCount(build.revision)) bad('build.revision must be a nonnegative integer');
    if (!(build.audit_id === null || isString(build.audit_id))) bad('build.audit_id must be a string or null');
    if (!isString(build.built_at)) bad('build.built_at must be a string');
    if (!isString(build.source_identity)) bad('build.source_identity must be a string');
    if (!BUILD_KINDS.includes(build.kind)) bad(`build.kind must be one of ${BUILD_KINDS.join(', ')}`);
  }
  const projection = input.projection;
  if (!isObject(projection)) { bad('projection must be an object'); done(); }
  if (![1, 2].includes(projection.projection_version)) bad('projection.projection_version must be 1 or 2');
  if (!isCount(projection.snapshot_revision)) bad('projection.snapshot_revision must be a nonnegative integer');
  if (!(projection.audit_id === null || isString(projection.audit_id))) bad('projection.audit_id must be a string or null');
  for (const field of ['nodes', 'connections', 'records', 'obligations', 'record_locations']) {
    if (!Array.isArray(projection[field])) bad(`projection.${field} must be an array`);
  }
  if (!isObject(projection.details)) bad('projection.details must be an object');
  if (!isObject(projection.summary)) bad('projection.summary must be an object');
  if (!isObject(projection.layout)) bad('projection.layout must be an object');
  done();

  const validateRef = (ref, where, pinned) => {
    if (!isObject(ref)) { bad(`${where} must be an object`); return null; }
    let ok = true;
    if (!isString(ref.collection) || !COLLECTION_PATTERN.test(ref.collection)) { bad(`${where}.collection must be a lowercase collection name`); ok = false; }
    if (!isString(ref.id) || !ID_PATTERN.test(ref.id)) { bad(`${where}.id must be a safe identifier`); ok = false; }
    if (pinned && !(Number.isInteger(ref.version) && ref.version >= 1)) { bad(`${where}.version must be a positive integer`); ok = false; }
    if (!pinned && ref.version !== undefined) { bad(`${where} must not carry a version`); ok = false; }
    return ok ? (pinned ? refKey(ref) : looseKey(ref)) : null;
  };
  const validateRefList = (list, where, pinned) => {
    if (!Array.isArray(list)) { bad(`${where} must be an array`); return []; }
    return list.map((ref, i) => validateRef(ref, `${where}[${i}]`, pinned));
  };
  const validateIdList = (list, where, options = {}) => {
    if (!Array.isArray(list)) { bad(`${where} must be an array`); return []; }
    const seen = new Set();
    list.forEach((id, i) => {
      if (!isString(id) || !ID_PATTERN.test(id)) { bad(`${where}[${i}] must be a safe identifier`); return; }
      if (options.unique && seen.has(id)) bad(`${where}[${i}] repeats ${id}`);
      seen.add(id);
      if (options.within && !options.within.has(id)) bad(`${where}[${i}] references unknown ${options.kind || 'identifier'} ${id}`);
    });
    return list;
  };
  const validateAssessment = (assessment, where) => {
    if (!isObject(assessment)) { bad(`${where}.assessment must be an object`); return; }
    if (!STATES.includes(assessment.state)) bad(`${where}.assessment.state must be one of ${STATES.join(', ')}`);
    if (!isString(assessment.label)) bad(`${where}.assessment.label must be a string`);
    if (!isString(assessment.explanation)) bad(`${where}.assessment.explanation must be a string`);
    validateRefList(assessment.check_refs, `${where}.assessment.check_refs`, true);
    validateIdList(assessment.finding_refs, `${where}.assessment.finding_refs`);
    validateIdList(assessment.missing_obligation_ids, `${where}.assessment.missing_obligation_ids`);
    if (!REVIEWS.includes(assessment.independent_review)) bad(`${where}.assessment.independent_review must be one of ${REVIEWS.join(', ')}`);
  };

  // Records first: everything else points at them.
  const records = new Map();
  projection.records.forEach((record, i) => {
    const where = `projection.records[${i}]`;
    if (!isObject(record)) { bad(`${where} must be an object`); return; }
    const key = validateRef(record.ref, `${where}.ref`, true);
    if (!key) return;
    if (records.has(key)) { bad(`${where} repeats record ${key}`); return; }
    if (!isObject(record.body)) { bad(`${where}.body must be an object`); return; }
    records.set(key, record);
  });
  const recordsByLoose = new Map();
  for (const record of records.values()) {
    const loose = looseKey(record.ref);
    if (!recordsByLoose.has(loose) || recordsByLoose.get(loose).ref.version < record.ref.version) recordsByLoose.set(loose, record);
  }
  const requirePinned = (ref, where) => {
    const key = validateRef(ref, where, true);
    if (key && !records.has(key)) bad(`${where} references ${key}, which is not in projection.records`);
    return key;
  };

  const obligations = new Map();
  projection.obligations.forEach((obligation, i) => {
    const where = `projection.obligations[${i}]`;
    if (!isObject(obligation)) { bad(`${where} must be an object`); return; }
    if (!isString(obligation.id) || !ID_PATTERN.test(obligation.id)) { bad(`${where}.id must be a safe identifier`); return; }
    if (obligations.has(obligation.id)) { bad(`${where} repeats obligation ${obligation.id}`); return; }
    validateRef(obligation.target, `${where}.target`, false);
    if (!isString(obligation.kind) || !obligation.kind.trim()) bad(`${where}.kind must be a nonempty string`);
    if (!ROLES.includes(obligation.role)) bad(`${where}.role must be one of ${ROLES.join(', ')}`);
    validateRefList(obligation.check_refs, `${where}.check_refs`, true);
    validateAssessment(obligation.assessment, where);
    obligations.set(obligation.id, obligation);
  });
  const obligationIds = new Set(obligations.keys());

  const details = new Map();
  Object.entries(projection.details).forEach(([key, detail]) => {
    const where = `projection.details[${JSON.stringify(key)}]`;
    if (!KEY_PATTERN.test(key)) { bad(`${where}: detail keys must be 1-512 characters without control characters`); return; }
    if (!isObject(detail)) { bad(`${where} must be an object`); return; }
    if (!Array.isArray(detail.record_refs)) bad(`${where}.record_refs must be an array`);
    else detail.record_refs.forEach((ref, i) => requirePinned(ref, `${where}.record_refs[${i}]`));
    if (!Array.isArray(detail.sections)) { bad(`${where}.sections must be an array`); return; }
    const sectionKeys = new Set();
    detail.sections.forEach((section, i) => {
      const at = `${where}.sections[${i}]`;
      if (!isObject(section)) { bad(`${at} must be an object`); return; }
      if (!isString(section.key) || !KEY_PATTERN.test(section.key)) { bad(`${at}.key must be 1-512 characters without control characters`); return; }
      if (sectionKeys.has(section.key)) bad(`${at}.key repeats ${section.key} within the detail`);
      sectionKeys.add(section.key);
      if (!isString(section.title)) bad(`${at}.title must be a string`);
      if (!SECTION_KINDS.includes(section.kind)) bad(`${at}.kind must be one of ${SECTION_KINDS.join(', ')}`);
      if (!(section.note === null || section.note === undefined || isString(section.note))) bad(`${at}.note must be a string or null`);
      if (!Array.isArray(section.record_refs)) bad(`${at}.record_refs must be an array`);
      else {
        const seen = new Set();
        section.record_refs.forEach((ref, j) => {
          const pinned = requirePinned(ref, `${at}.record_refs[${j}]`);
          if (pinned && seen.has(pinned)) bad(`${at}.record_refs[${j}] repeats ${pinned} within the section`);
          if (pinned) seen.add(pinned);
        });
      }
      validateIdList(section.obligation_ids, `${at}.obligation_ids`, { unique: true, within: obligationIds, kind: 'obligation' });
    });
    details.set(key, detail);
  });

  const nodes = new Map();
  projection.nodes.forEach((node, i) => {
    const where = `projection.nodes[${i}]`;
    if (!isObject(node)) { bad(`${where} must be an object`); return; }
    if (!isString(node.id) || !ID_PATTERN.test(node.id)) { bad(`${where}.id must be a safe identifier`); return; }
    if (nodes.has(node.id)) { bad(`${where} repeats node ${node.id}`); return; }
    validateRef(node.item_ref, `${where}.item_ref`, true);
    if (!isString(node.kind) || !Object.hasOwn(KINDS, node.kind)) bad(`${where}.kind ${JSON.stringify(node.kind)} is not one of ${Object.keys(KINDS).join(', ')}`);
    if (!isString(node.label) || !node.label.trim()) bad(`${where}.label must be a nonempty string`);
    if (!isString(node.caption)) bad(`${where}.caption must be a string`);
    validateAssessment(node.assessment, where);
    if (!isString(node.detail_key) || !details.has(node.detail_key)) bad(`${where}.detail_key must name a key of projection.details`);
    nodes.set(node.id, node);
  });

  const connections = new Map();
  projection.connections.forEach((connection, i) => {
    const where = `projection.connections[${i}]`;
    if (!isObject(connection)) { bad(`${where} must be an object`); return; }
    if (!isString(connection.id) || !ID_PATTERN.test(connection.id)) { bad(`${where}.id must be a safe identifier`); return; }
    if (connections.has(connection.id)) { bad(`${where} repeats connection ${connection.id}`); return; }
    if (!isString(connection.from) || !nodes.has(connection.from)) bad(`${where}.from must name a node`);
    if (!isString(connection.to) || !nodes.has(connection.to)) bad(`${where}.to must name a node`);
    validateIdList(connection.primary_use_ids, `${where}.primary_use_ids`, { unique: true });
    if (!Array.isArray(connection.groups)) bad(`${where}.groups must be an array`);
    else connection.groups.forEach((group, j) => {
      const at = `${where}.groups[${j}]`;
      if (!isObject(group)) { bad(`${at} must be an object`); return; }
      for (const field of ['argument_id', 'group_id']) {
        if (!(group[field] === null || (isString(group[field]) && ID_PATTERN.test(group[field])))) bad(`${at}.${field} must be a safe identifier or null`);
      }
      validateIdList(group.use_ids, `${at}.use_ids`);
      validateIdList(group.obligation_ids, `${at}.obligation_ids`, { within: obligationIds, kind: 'obligation' });
      validateAssessment(group.assessment, at);
    });
    validateRefList(connection.support_refs, `${where}.support_refs`, false);
    validateIdList(connection.obligation_ids, `${where}.obligation_ids`, { within: obligationIds, kind: 'obligation' });
    validateRefList(connection.context_refs, `${where}.context_refs`, false);
    validateAssessment(connection.assessment, where);
    if (!isString(connection.detail_key) || !details.has(connection.detail_key)) bad(`${where}.detail_key must name a key of projection.details`);
    connections.set(connection.id, connection);
  });

  projection.record_locations.forEach((location, i) => {
    const where = `projection.record_locations[${i}]`;
    if (!isObject(location)) { bad(`${where} must be an object`); return; }
    requirePinned(location.ref, `${where}.ref`);
    if (!isString(location.detail_key) || !details.has(location.detail_key)) { bad(`${where}.detail_key must name a key of projection.details`); return; }
    const detail = details.get(location.detail_key);
    if (!isString(location.section_key) || !Array.isArray(detail.sections) || !detail.sections.some((section) => isObject(section) && section.key === location.section_key)) {
      bad(`${where}.section_key must name a section of detail ${JSON.stringify(location.detail_key)}`);
    }
  });

  if (projection.projection_version === 2 && projection.worklist === undefined) bad('projection.worklist must be present in version 2');
  if (projection.worklist !== undefined && projection.worklist !== null) {
    const work = projection.worklist;
    if (!isObject(work)) bad('projection.worklist must be an object or null');
    else {
      if (work.revision !== projection.snapshot_revision) bad('projection.worklist.revision must match the snapshot');
      if (typeof work.analysis_complete !== 'boolean') bad('projection.worklist.analysis_complete must be boolean');
      if (!Array.isArray(work.tasks)) bad('projection.worklist.tasks must be an array');
      else work.tasks.forEach((task, i) => {
        const where = `projection.worklist.tasks[${i}]`;
        if (!isObject(task)) { bad(`${where} must be an object`); return; }
        if (!obligationIds.has(task.id)) bad(`${where}.id must name a represented obligation`);
        if (!isString(task.label)) bad(`${where}.label must be a string`);
        if (!ROLES.includes(task.role)) bad(`${where}.role must be a checking role`);
        if (typeof task.required !== 'boolean') bad(`${where}.required must be boolean`);
        if (!['ready', 'waiting', 'needs_coordinator', 'satisfied'].includes(task.state)) bad(`${where}.state is unknown`);
        if (!(task.next_action === null || isString(task.next_action))) bad(`${where}.next_action must be text or null`);
        if (task.location !== null) {
          const location = task.location;
          if (!isObject(location)) { bad(`${where}.location must be an object or null`); return; }
          requirePinned(location.ref, `${where}.location.ref`);
          const detail = details.get(location.detail_key);
          const section = detail && detail.sections.find(row => row.key === location.section_key);
          if (!section) bad(`${where}.location must name an existing reader section`);
          if (location.obligation_id !== undefined && (location.obligation_id !== task.id || !section || !section.obligation_ids.includes(task.id)))
            bad(`${where}.location must name this task's actual obligation`);
        }
      });
      if (!Array.isArray(work.coordinator_actions) || work.coordinator_actions.some(action => !isObject(action) || !isString(action.message)))
        bad('projection.worklist.coordinator_actions must contain readable messages');
    }
  }

  const summary = projection.summary;
  if (!isObject(summary.scope)) bad('projection.summary.scope must be an object');
  else {
    if (!isString(summary.scope.mode)) bad('projection.summary.scope.mode must be a string');
    validateRefList(summary.scope.target_refs, 'projection.summary.scope.target_refs', false);
    if (!Array.isArray(summary.scope.exclusions)) bad('projection.summary.scope.exclusions must be an array');
  }
  if (!isObject(summary.progress)) bad('projection.summary.progress must be an object');
  else {
    if (typeof summary.progress.process_complete !== 'boolean') bad('projection.summary.progress.process_complete must be a boolean');
    for (const field of PROGRESS_FIELDS) if (!isCount(summary.progress[field])) bad(`projection.summary.progress.${field} must be a nonnegative integer`);
  }
  if (!isObject(summary.findings)) bad('projection.summary.findings must be an object');
  else {
    for (const field of ['open', 'resolved', 'superseded']) if (!isCount(summary.findings[field])) bad(`projection.summary.findings.${field} must be a nonnegative integer`);
    validateIdList(summary.findings.refs, 'projection.summary.findings.refs', { unique: true });
  }
  validateIdList(summary.source_limits, 'projection.summary.source_limits', { unique: true });
  if (summary.limitations !== undefined && (!Array.isArray(summary.limitations) || !summary.limitations.every(isString))) bad('projection.summary.limitations must be an array of strings');
  if (!(summary.published_revision === null || isCount(summary.published_revision))) bad('projection.summary.published_revision must be a nonnegative integer or null');

  const layout = projection.layout;
  if (!LAYOUT_MODES.includes(layout.mode)) bad(`projection.layout.mode must be one of ${LAYOUT_MODES.join(', ')}`);
  if (!Array.isArray(layout.reasons) || layout.reasons.some((reason) => !isString(reason))) bad('projection.layout.reasons must be an array of strings');

  const display = input.display === undefined ? {} : input.display;
  if (!isObject(display)) bad('display must be an object');
  const refs = isObject(display) && display.refs !== undefined ? display.refs : {};
  if (!isObject(refs)) bad('display.refs must be an object');
  const fragments = new Map();
  if (isObject(refs)) {
    Object.entries(refs).forEach(([key, value]) => {
      const where = `display.refs[${JSON.stringify(key)}]`;
      if (!records.has(key)) { bad(`${where} does not match any projection record (expected COLLECTION:ID:VERSION)`); return; }
      if (!isObject(value)) { bad(`${where} must be an object`); return; }
      for (const [field, fragment] of Object.entries(value)) {
        if (!DISPLAY_KEYS.includes(field)) bad(`${where}.${field} is not a known display fragment (${DISPLAY_KEYS.join(', ')})`);
        else if (field === 'conditions_html') {
          if (!Array.isArray(fragment) || fragment.some((entry) => !isString(entry))) bad(`${where}.conditions_html must be an array of strings`);
        } else if (!isString(fragment)) bad(`${where}.${field} must be a string`);
      }
      fragments.set(key, value);
    });
  }
  done();

  return {
    title: input.title,
    build,
    projection,
    nodeList: projection.nodes,
    connectionList: projection.connections,
    nodes,
    connections,
    records,
    recordsByLoose,
    obligations,
    details,
    layout,
    summary,
    fragments,
  };
}

// ---------------------------------------------------------------------------
// Layout (dag mode)
// ---------------------------------------------------------------------------

function findCycle(stuckIds, outgoing) {
  const stuck = new Set(stuckIds);
  const state = new Map();
  const stack = [];
  const visit = (id) => {
    state.set(id, 1);
    stack.push(id);
    for (const connection of outgoing.get(id)) {
      if (!stuck.has(connection.to)) continue;
      const seen = state.get(connection.to) || 0;
      if (seen === 1) return [...stack.slice(stack.indexOf(connection.to)), connection.to];
      if (seen === 0) {
        const found = visit(connection.to);
        if (found) return found;
      }
    }
    stack.pop();
    state.set(id, 2);
    return null;
  };
  for (const id of stuckIds) {
    if (!state.get(id)) {
      const found = visit(id);
      if (found) return found;
    }
  }
  return stuckIds;
}

// Longest-path layers keep prerequisites to the left. Ordering uses stable
// barycentres; coordinates are never authored.
export function layoutGraph(model) {
  const nodes = new Map(model.nodeList.map((node, order) => [node.id, { id: node.id, order, rank: 0, source: node }]));
  const incoming = new Map(model.nodeList.map((node) => [node.id, []]));
  const outgoing = new Map(model.nodeList.map((node) => [node.id, []]));
  model.connectionList.forEach((connection) => { incoming.get(connection.to).push(connection); outgoing.get(connection.from).push(connection); });
  const remaining = new Map([...incoming].map(([id, list]) => [id, list.length]));
  const queue = model.nodeList.filter((node) => !remaining.get(node.id)).map((node) => node.id);
  let visited = 0;
  for (let index = 0; index < queue.length; index += 1) {
    const id = queue[index];
    visited += 1;
    for (const connection of outgoing.get(id)) {
      const target = nodes.get(connection.to);
      target.rank = Math.max(target.rank, nodes.get(id).rank + 1);
      remaining.set(connection.to, remaining.get(connection.to) - 1);
      if (!remaining.get(connection.to)) queue.push(connection.to);
    }
  }
  if (visited !== nodes.size) {
    const stuck = [...remaining].filter(([, count]) => count > 0).map(([id]) => id);
    const cycle = findCycle(stuck, outgoing);
    fail('layout', 'Connections form a cycle; a dag layout is impossible and no index fallback is applied.', [`cycle: ${cycle.join(' -> ')}`, `nodes blocked by the cycle: ${stuck.join(', ')}`]);
  }
  const components = [];
  const assigned = new Set();
  nodes.forEach((start) => {
    if (assigned.has(start.id)) return;
    const ids = [start.id];
    assigned.add(start.id);
    for (let i = 0; i < ids.length; i += 1) {
      for (const connection of [...incoming.get(ids[i]), ...outgoing.get(ids[i])]) {
        const other = connection.from === ids[i] ? connection.to : connection.from;
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
          const parents = incoming.get(node.id).map((connection) => nodes.get(connection.from).position);
          return parents.length ? parents.reduce((sum, value) => sum + value, 0) / parents.length : node.position;
        };
        ranks[r].sort((a, b) => centre(a) - centre(b) || a.order - b.order);
        ranks[r].forEach((node, index) => { node.position = index; });
      }
    }
    components.push({ members, ranks, rowCount: Math.max(...ranks.map((rank) => rank.length)) });
  });
  const longConnections = model.connectionList.filter((connection) => nodes.get(connection.to).rank > nodes.get(connection.from).rank + 1);
  let top = BOX.margin;
  components.forEach((component, componentIndex) => {
    component.top = top;
    component.nodeTop = top + (components.length > 1 ? 32 : 0);
    component.ranks.forEach((rank, r) => rank.forEach((node, index) => {
      node.x = BOX.margin + r * BOX.column;
      node.y = component.nodeTop + index * BOX.row;
      node.width = BOX.w;
      node.height = BOX.h;
      node.component = componentIndex;
    }));
    component.bottom = component.nodeTop + (component.rowCount - 1) * BOX.row + BOX.h;
    top = component.bottom + 68;
  });
  const rankCount = Math.max(...components.map((component) => component.ranks.length));
  return {
    nodes, components, incoming, outgoing, longConnections,
    width: BOX.margin * 2 + (rankCount - 1) * BOX.column + BOX.w,
    height: top - 68 + BOX.margin,
  };
}

function roundedPath(points, radius = 8) {
  const commands = [`M ${fmt(points[0][0])} ${fmt(points[0][1])}`];
  for (let i = 1; i < points.length - 1; i += 1) {
    const [px, py] = points[i - 1], [cx, cy] = points[i], [nx, ny] = points[i + 1];
    const before = Math.hypot(cx - px, cy - py), after = Math.hypot(nx - cx, ny - cy);
    const r = Math.min(radius, before / 2, after / 2);
    if (r < 1) { commands.push(`L ${fmt(cx)} ${fmt(cy)}`); continue; }
    commands.push(`L ${fmt(cx - (cx - px) / before * r)} ${fmt(cy - (cy - py) / before * r)}`);
    commands.push(`Q ${fmt(cx)} ${fmt(cy)} ${fmt(cx + (nx - cx) / after * r)} ${fmt(cy + (ny - cy) / after * r)}`);
  }
  commands.push(`L ${fmt(points.at(-1)[0])} ${fmt(points.at(-1)[1])}`);
  return commands.join(' ');
}

function edgePoints(connection, graph) {
  const a = graph.nodes.get(connection.from), b = graph.nodes.get(connection.to);
  const outs = graph.outgoing.get(a.id), ins = graph.incoming.get(b.id);
  const port = (node, list) => round2(node.y + 13 + (list.indexOf(connection) + 1) / (list.length + 1) * (BOX.h - 26));
  const start = [a.x + BOX.w, port(a, outs)], end = [b.x, port(b, ins)];
  const longIndex = graph.longConnections.indexOf(connection);
  if (longIndex >= 0) {
    const component = graph.components[a.component];
    const target = (start[1] + end[1]) / 2;
    const corridors = Array.from({ length: component.rowCount }, (_, row) => component.nodeTop + row * BOX.row + BOX.h + (BOX.row - BOX.h) / 2);
    const rail = corridors.reduce((best, value) => (Math.abs(value - target) < Math.abs(best - target) ? value : best));
    const exit = round2(start[0] + 18 + (outs.indexOf(connection) + 1) / (outs.length + 1) * 24);
    const enter = round2(end[0] - 18 - (ins.indexOf(connection) + 1) / (ins.length + 1) * 24);
    return [start, [exit, start[1]], [exit, rail], [enter, rail], [enter, end[1]], end];
  }
  const sameGap = sameGapConnections(graph, a, b);
  const channel = round2(start[0] + 25 + (sameGap.indexOf(connection) + 1) / (sameGap.length + 1) * (BOX.column - BOX.w - 50));
  return [start, [channel, start[1]], [channel, end[1]], end];
}

function sameGapConnections(graph, a, b) {
  return [...graph.outgoing.values()].flat().filter((entry) => {
    const from = graph.nodes.get(entry.from), to = graph.nodes.get(entry.to);
    return from.component === a.component && from.rank === a.rank && to.rank === b.rank;
  });
}

function captionLines(text, limit = 25) {
  const words = String(text || '').split(/\s+/u).filter(Boolean);
  if (!words.length) return [];
  const lines = [''];
  for (const word of words) {
    const at = lines.length - 1;
    if (lines[at] && textUnits(`${lines[at]} ${word}`) > limit) lines.push(word);
    else lines[at] += (lines[at] ? ' ' : '') + word;
  }
  const clip = (line) => (textUnits(line) > limit ? `${line.slice(0, limit - 1)}…` : line);
  if (lines.length > 2) return [clip(lines[0]), `${lines[1].slice(0, limit - 1)}…`];
  return lines.map(clip);
}

// ---------------------------------------------------------------------------
// HTML fragments shared by the graph, the index and the details
// ---------------------------------------------------------------------------

function stateBadge(state, text) {
  return `<span class="proof-state-badge s-${esc(state)}"><span class="proof-state-glyph" aria-hidden="true">${esc(STATE_GLYPHS[state])}</span>${esc(text === undefined ? STATE_LABELS[state] : text)}</span>`;
}

function kindBadge(kind) {
  return `<span class="proof-kind-badge k-${esc(kind)}">${esc(KINDS[kind][0])}</span>`;
}

function reviewNote(review) {
  return `<span class="proof-review r-${esc(review)}">Independent review: ${esc(REVIEW_LABELS[review])}</span>`;
}

// Wrap MathML so long formulas scroll inside their box instead of widening it.
function prepareFragment(html) {
  return String(html).replace(/<math\b[\s\S]*?<\/math>/gi, (match) => `<span class="proof-formula${/display\s*=\s*["']block["']/i.test(match.slice(0, match.indexOf('>') + 1)) ? ' proof-formula-block' : ''}">${match}</span>`);
}

class Renderer {
  constructor(model, graph) {
    this.model = model;
    this.graph = graph;
    this.detailIndex = new Map([...model.details.keys()].map((key, i) => [key, i + 1]));
    this.locationsByPinned = new Map();
    this.locationsByLoose = new Map();
    for (const location of model.projection.record_locations) {
      const pinned = refKey(location.ref);
      if (!this.locationsByPinned.has(pinned)) this.locationsByPinned.set(pinned, location);
      const loose = looseKey(location.ref);
      if (!this.locationsByLoose.has(loose)) this.locationsByLoose.set(loose, location);
    }
    this.owners = new Map();
    for (const node of model.nodeList) this.pushOwner(node.detail_key, { type: 'node', node });
    for (const connection of model.connectionList) this.pushOwner(connection.detail_key, { type: 'connection', connection });
  }

  pushOwner(key, owner) {
    if (!this.owners.has(key)) this.owners.set(key, []);
    this.owners.get(key).push(owner);
  }

  detailId(key) { return `proof-detail-${this.detailIndex.get(key)}`; }

  nodeLabel(id) { const node = this.model.nodes.get(id); return node ? node.label : id; }

  jump(key, text, options = {}) {
    const attrs = [`class="proof-jump${options.className ? ` ${options.className}` : ''}"`, `href="#${esc(this.detailId(key))}"`, `data-jump-detail="${esc(key)}"`];
    if (options.section) attrs.push(`data-jump-section="${esc(options.section)}"`);
    if (options.record) attrs.push(`data-jump-record="${esc(options.record)}"`);
    return `<a ${attrs.join(' ')}>${text}</a>`;
  }

  // Link to the first location of a record (pinned or loose), else plain text.
  recordLink(ref, pinned) {
    const key = pinned ? refKey(ref) : looseKey(ref);
    const location = pinned ? this.locationsByPinned.get(key) : this.locationsByLoose.get(key);
    const text = `<code class="proof-ref-text">${esc(key)}</code>`;
    if (!location) return `<span class="proof-ref">${text}</span>`;
    return this.jump(location.detail_key, text, { section: location.section_key, record: refKey(location.ref), className: 'proof-ref' });
  }

  idLink(id, collection) {
    return this.recordLink({ collection, id }, false);
  }

  assessmentBlock(assessment, options = {}) {
    const parts = [`<div class="proof-assessment s-${esc(assessment.state)}">`];
    parts.push(`<p class="proof-assessment-head">${stateBadge(assessment.state)} <strong class="proof-assessment-label">${esc(assessment.label)}</strong></p>`);
    if (assessment.explanation) parts.push(`<p class="proof-assessment-text">${esc(assessment.explanation)}</p>`);
    parts.push(`<p class="proof-assessment-meta">${reviewNote(assessment.independent_review)}</p>`);
    if (!options.compact) {
      if (assessment.check_refs.length) parts.push(`<p class="proof-assessment-meta">Checks: ${assessment.check_refs.map((ref) => this.recordLink(ref, true)).join(', ')}</p>`);
      if (assessment.finding_refs.length) parts.push(`<p class="proof-assessment-meta">Findings: ${assessment.finding_refs.map((id) => this.idLink(id, 'findings')).join(', ')}</p>`);
      if (assessment.missing_obligation_ids.length) parts.push(`<p class="proof-assessment-meta">Missing obligations: ${assessment.missing_obligation_ids.map((id) => `<code>${esc(id)}</code>`).join(', ')}</p>`);
    }
    parts.push('</div>');
    return parts.join('');
  }

  // ----- summary -----------------------------------------------------------

  summaryHtml() {
    const { model } = this;
    const { summary, projection } = model;
    const tally = (list) => Object.fromEntries(STATES.map((state) => [state, list.filter((entry) => entry.assessment.state === state).length]));
    const nodeTally = tally(model.nodeList), connectionTally = tally(model.connectionList);
    const count = (name, value) => `<span class="proof-count" data-proof-count="${name}">${value}</span>`;
    const stateList = (prefix, tallies) => `<ul class="proof-state-list">${STATES.map((state) => `<li class="s-${state}">${stateBadge(state)} ${count(`${prefix}.${state}`, tallies[state])}</li>`).join('')}</ul>`;
    const progress = summary.progress;
    const findingItems = summary.findings.refs.map((id) => {
      const record = model.recordsByLoose.get(`findings:${id}`);
      const body = record ? record.body : null;
      const meta = body ? [body.category, body.lifecycle].filter(isString).map((value) => `<span class="proof-pill">${esc(value)}</span>`).join(' ') : '<span class="proof-muted">not in projection records</span>';
      const text = body && isString(body.description) ? `<span class="proof-finding-text">${esc(body.description.length > 160 ? `${body.description.slice(0, 159)}…` : body.description)}</span>` : '';
      return `<li data-proof-finding="${esc(id)}">${this.idLink(id, 'findings')} ${meta} ${text}</li>`;
    });
    const limitItems = summary.source_limits.map((id) => {
      const record = model.recordsByLoose.get(`source_issues:${id}`);
      const body = record ? record.body : null;
      const meta = body ? [body.category, body.lifecycle].filter(isString).map((value) => `<span class="proof-pill">${esc(value)}</span>`).join(' ') : '';
      const text = body && isString(body.description) ? `<span class="proof-finding-text">${esc(body.description.length > 160 ? `${body.description.slice(0, 159)}…` : body.description)}</span>` : '';
      return `<li data-proof-source-limit="${esc(id)}">${this.idLink(id, 'source_issues')} ${meta} ${text}</li>`;
    });
    const scope = summary.scope;
    const exclusionItems = scope.exclusions.map((entry) => `<li>${isObject(entry) && isString(entry.collection) && isString(entry.id) ? this.recordLink(entry, false) : `<span class="proof-text">${esc(isString(entry) ? entry : JSON.stringify(entry))}</span>`}</li>`);
    return `<section id="proof-summary" class="proof-panel" aria-labelledby="proof-summary-heading">
<h2 id="proof-summary-heading">Audit summary</h2>
<div class="proof-summary-grid">
<div class="proof-card">
<h3>Process</h3>
<p class="proof-process ${progress.process_complete ? 'is-complete' : 'is-incomplete'}" data-proof-process-complete="${progress.process_complete ? 'true' : 'false'}">${progress.process_complete ? 'Audit process complete' : 'Audit process incomplete'}</p>
<dl class="proof-kv">
<dt>Required obligations</dt><dd>${count('progress.required_obligations', progress.required_obligations)}</dd>
<dt>Completed current obligations</dt><dd>${count('progress.completed_current_obligations', progress.completed_current_obligations)}</dd>
<dt>Draft checks</dt><dd>${count('progress.draft_checks', progress.draft_checks)}</dd>
<dt>Major results</dt><dd>${count('progress.major_results', progress.major_results)}</dd>
<dt>Source-unbound items</dt><dd>${count('progress.source_unbound_items', progress.source_unbound_items)}</dd>
</dl>
${summary.limitations?.length ? `<h4>Limits on completion</h4><ul>${summary.limitations.map((note) => `<li data-proof-limitation>${esc(note)}</li>`).join('')}</ul>` : ''}
</div>
<div class="proof-card">
<h3>Nodes by state</h3>
${stateList('nodes', nodeTally)}
<h3>Connections by state</h3>
${stateList('connections', connectionTally)}
</div>
<div class="proof-card">
<h3>Findings</h3>
<dl class="proof-kv proof-kv-inline">
<dt>Open</dt><dd>${count('findings.open', summary.findings.open)}</dd>
<dt>Resolved</dt><dd>${count('findings.resolved', summary.findings.resolved)}</dd>
<dt>Superseded</dt><dd>${count('findings.superseded', summary.findings.superseded)}</dd>
</dl>
${findingItems.length ? `<ul class="proof-finding-list">${findingItems.join('')}</ul>` : '<p class="proof-muted">No findings listed.</p>'}
<h3>Source limits</h3>
${limitItems.length ? `<ul class="proof-finding-list">${limitItems.join('')}</ul>` : '<p class="proof-muted">No source limits listed.</p>'}
</div>
<div class="proof-card">
<h3>Scope</h3>
<dl class="proof-kv">
<dt>Mode</dt><dd>${esc(scope.mode)}</dd>
<dt>Targets</dt><dd>${scope.target_refs.length ? scope.target_refs.map((ref) => this.recordLink(ref, false)).join(', ') : '<span class="proof-muted">whole paper</span>'}</dd>
<dt>Exclusions</dt><dd>${exclusionItems.length ? `<ul class="proof-plain-list">${exclusionItems.join('')}</ul>` : '<span class="proof-muted">none</span>'}</dd>
<dt>Snapshot revision</dt><dd>${projection.snapshot_revision}</dd>
<dt>Published revision</dt><dd>${summary.published_revision === null ? '<span class="proof-muted">not published</span>' : summary.published_revision}</dd>
<dt>Audit</dt><dd>${projection.audit_id === null ? '<span class="proof-muted">none</span>' : `<code>${esc(projection.audit_id)}</code>`}</dd>
</dl>
</div>
</div>
</section>`;
  }

  // ----- graph (dag) -------------------------------------------------------

  svgHtml() {
    const { model, graph } = this;
    const badges = [];
    const edges = model.connectionList.map((connection, edgeIndex) => {
      const points = edgePoints(connection, graph);
      const state = connection.assessment.state;
      const d = roundedPath(points);
      const labels = [];
      if (connection.primary_use_ids.length > 1) labels.push(`${connection.primary_use_ids.length} applications`);
      if (connection.groups.length) labels.push(`${connection.groups.length} ${connection.groups.length === 1 ? 'group' : 'groups'}`);
      const description = `${this.nodeLabel(connection.from)} to ${this.nodeLabel(connection.to)}. ${STATE_LABELS[state]}: ${connection.assessment.label}.${labels.length ? ` ${labels.join(', ')}.` : ''}`;
      if (labels.length) badges.push({ connection, description: `${description} Select for details.`, labels, points });
      return `<path data-edge-id="${esc(connection.id)}" data-edge-key="${edgeIndex}" data-edge-label="${esc(description)}" data-edge-from="${esc(connection.from)}" data-edge-to="${esc(connection.to)}" data-edge-state="${esc(state)}" data-detail-key="${esc(connection.detail_key)}" class="proof-edge s-${esc(state)}" d="${d}" marker-end="url(#proof-arrow-${esc(state)})"><title>${esc(description)}</title></path>`;
    });
    const ordered = [...graph.nodes.values()].sort((a, b) => a.rank - b.rank || a.y - b.y || a.order - b.order);
    const nodes = ordered.map((node) => {
      const source = node.source;
      const state = source.assessment.state;
      const review = source.assessment.independent_review;
      const cx = node.x + BOX.w / 2;
      const caption = captionLines(source.caption);
      const labelSize = Math.max(12, Math.min(14, 150 / Math.max(1, textUnits(source.label)) / 0.61));
      const squeeze = textUnits(source.label) * labelSize * 0.61 > 154 ? ' textLength="154" lengthAdjust="spacingAndGlyphs"' : '';
      const aria = `${source.label}. ${KINDS[source.kind][0]}. ${STATE_LABELS[state]}: ${source.assessment.label}. Independent review ${REVIEW_LABELS[review]}. Press Enter for details.`;
      const tooltip = `${source.label}${source.caption ? `: ${source.caption}` : ''} (${KINDS[source.kind][0]}, ${STATE_LABELS[state].toLowerCase()})`;
      const reviewMark = review === 'not_required' ? '' : `<g class="proof-node-review r-${esc(review)}" aria-hidden="true"><rect x="${node.x + BOX.w - 24}" y="${node.y + BOX.h - 8}" width="26" height="14" rx="7"/><text x="${node.x + BOX.w - 11}" y="${node.y + BOX.h + 2.5}" text-anchor="middle" font-size="8.5">IR</text></g>`;
      return `<g data-node-id="${esc(source.id)}" data-node-label="${esc(source.label)}" data-node-sublabel="${esc(source.caption)}" data-node-state="${esc(state)}" data-node-kind="${esc(source.kind)}" data-detail-key="${esc(source.detail_key)}" data-node-rank="${node.rank}" data-node-x="${node.x}" data-node-y="${node.y}" tabindex="0" role="button" aria-pressed="false" aria-label="${esc(aria)}" class="proof-node k-${esc(source.kind)} s-${esc(state)}">
<title>${esc(tooltip)}</title>
<rect class="proof-node-ring" x="${node.x - 3}" y="${node.y - 3}" width="${BOX.w + 6}" height="${BOX.h + 6}" rx="9"/>
<rect class="proof-node-box" x="${node.x}" y="${node.y}" width="${BOX.w}" height="${BOX.h}" rx="6"/>
<text class="proof-node-label" data-node-label="${esc(source.label)}" x="${cx}" y="${node.y + 23}" font-size="${fmt(labelSize)}" font-weight="600" text-anchor="middle"${squeeze}>${esc(source.label)}</text>
${caption.map((line, i) => `<text class="proof-node-caption" data-detail="context" x="${cx}" y="${node.y + 40 + 13 * i}" font-size="11" text-anchor="middle">${esc(line)}</text>`).join('\n')}
<g class="proof-node-state" aria-hidden="true"><circle cx="${node.x + BOX.w - 2}" cy="${node.y + 2}" r="8.5"/><text x="${node.x + BOX.w - 2}" y="${node.y + 5.5}" text-anchor="middle" font-size="10" font-weight="700">${esc(STATE_GLYPHS[state])}</text></g>
${reviewMark}
</g>`;
    });
    const badgeSvg = this.badgesHtml(badges);
    const components = graph.components.length > 1 ? graph.components.map((component) => {
      const ends = component.members.filter((node) => !graph.outgoing.get(node.id).length).map((node) => node.source.label);
      return `<g class="proof-component" aria-hidden="true"><path d="M 22 ${component.top + 16} H ${graph.width - 22}"/><text x="${BOX.margin}" y="${component.top + 9}" font-size="11">Linked argument: ${esc(ends.join(', '))}</text></g>`;
    }).join('\n') : '';
    const markers = STATES.map((state) => `<marker id="proof-arrow-${state}" markerWidth="8" markerHeight="6" refX="7.2" refY="3" orient="auto" markerUnits="userSpaceOnUse"><path d="M0 0 L8 3 L0 6 Z" class="proof-arrowhead s-${state}"/></marker>`).join('');
    return `<svg class="proof-svg" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${graph.width} ${graph.height}" width="${graph.width}" height="${graph.height}" role="group" aria-labelledby="proof-graph-heading" aria-describedby="proof-graph-desc">
<defs>${markers}<pattern id="proof-grid" width="40" height="40" patternUnits="userSpaceOnUse"><path d="M40 0 L0 0 0 40" class="proof-grid-line" stroke-width="0.5"/></pattern></defs>
<rect class="proof-grid-fill" width="100%" height="100%" fill="url(#proof-grid)"/>
${components}
${edges.join('\n')}
${badgeSvg}
${nodes.join('\n')}
</svg>`;
  }

  badgesHtml(badges) {
    const { graph } = this;
    const occupied = [...graph.nodes.values()].map((node) => ({ x: node.x - 6, y: node.y - 6, w: BOX.w + 12, h: BOX.h + 12 }));
    const intersects = (a, b) => a.x < b.x + b.w + 3 && a.x + a.w + 3 > b.x && a.y < b.y + b.h + 3 && a.y + a.h + 3 > b.y;
    return badges.map(({ connection, description, labels, points }) => {
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
        const anchor = points[Math.floor(points.length / 2)];
        for (let y = 18 + h / 2; !placed && y < graph.height - h / 2; y += h + 7) {
          for (let x = 8 + w / 2; !placed && x < graph.width - w / 2; x += w + 8) {
            const rect = { x: x - w / 2, y: y - h / 2, w, h };
            if (!occupied.some((entry) => intersects(rect, entry))) placed = { x, y, rect, anchor };
          }
        }
      }
      if (!placed) {
        const anchor = points[Math.floor(points.length / 2)];
        const x = Math.min(graph.width - w / 2 - 8, Math.max(w / 2 + 8, anchor[0])), y = graph.height + h / 2;
        placed = { x, y, anchor, rect: { x: x - w / 2, y: y - h / 2, w, h } };
        graph.height += h + 12;
      }
      occupied.push(placed.rect);
      const { x, y, anchor } = placed;
      const leader = Math.hypot(x - anchor[0], y - anchor[1]) > 3;
      const state = connection.assessment.state;
      return `${leader ? `<g aria-hidden="true" class="proof-badge-decoration"><path class="proof-badge-leader" d="M ${fmt(anchor[0])} ${fmt(anchor[1])} L ${fmt(x)} ${fmt(y)}"/><circle class="proof-badge-anchor" cx="${fmt(anchor[0])}" cy="${fmt(anchor[1])}" r="2"/></g>` : ''}<g class="proof-edge-badge s-${esc(state)}" data-edge-badge="${esc(connection.id)}" data-detail-key="${esc(connection.detail_key)}" tabindex="0" role="button" aria-label="${esc(description)}"><title>${esc(description)}</title><rect x="${fmt(x - w / 2)}" y="${fmt(y - h / 2)}" width="${fmt(w)}" height="${h}" rx="4"/>${labels.map((label, i) => `<text x="${fmt(x)}" y="${fmt(y - (labels.length - 1) * 7 + i * 14 + 3)}" text-anchor="middle">${esc(label)}</text>`).join('')}</g>`;
    }).join('\n');
  }

  legendHtml() {
    const kinds = Object.entries(KINDS).map(([kind, [name]]) => `<li class="k-${kind}"><span class="proof-legend-swatch proof-legend-kind"></span>${esc(name)}</li>`).join('');
    const states = STATES.map((state) => `<li>${stateBadge(state)} <span class="proof-legend-note">${esc(state === 'green' ? 'ring solid' : state === 'red' ? 'ring thick' : state === 'amber' ? 'ring dashed' : 'ring dotted')}</span></li>`).join('');
    return `<div class="proof-legend">
<ul class="proof-legend-list" aria-label="Item kinds">${kinds}</ul>
<ul class="proof-legend-list" aria-label="Assessment states">${states}</ul>
<p class="proof-legend-note">Boxes are tinted by item kind; the ring, corner glyph and edge colour show the recorded assessment state. An <span class="proof-ir-sample">IR</span> tag marks items whose independent review is pending, complete, disputed or compromised. Edge badges mark connections with several applications or hidden-claim groups. Keyboard: Tab or arrow keys move between items, Enter opens details, Escape returns to the graph.</p>
</div>`;
  }

  graphSectionHtml() {
    const reasons = this.model.layout.reasons.length ? `<ul class="proof-reasons">${this.model.layout.reasons.map((reason) => `<li>${esc(reason)}</li>`).join('')}</ul>` : '';
    return `<section id="proof-graph" class="proof-panel" aria-labelledby="proof-graph-heading">
<div class="proof-panel-head"><h2 id="proof-graph-heading">Dependency graph</h2><p id="proof-graph-desc" class="proof-muted">Prerequisites on the left, results on the right. Arrows point from what is used to what uses it. States are copied from the audit record; nothing here is computed by the renderer.</p></div>
${reasons}
<div class="proof-canvas" tabindex="-1">
${this.svgHtml()}
</div>
${this.legendHtml()}
</section>`;
  }

  // ----- index (fallback) --------------------------------------------------

  indexHtml() {
    const { model } = this;
    const incoming = new Map(model.nodeList.map((node) => [node.id, 0]));
    const outgoing = new Map(model.nodeList.map((node) => [node.id, 0]));
    for (const connection of model.connectionList) {
      incoming.set(connection.to, incoming.get(connection.to) + 1);
      outgoing.set(connection.from, outgoing.get(connection.from) + 1);
    }
    const items = model.nodeList.map((node) => {
      const state = node.assessment.state;
      return `<article data-proof-index-item="${esc(node.id)}" data-node-state="${esc(state)}" data-node-kind="${esc(node.kind)}" class="proof-index-card k-${esc(node.kind)} s-${esc(state)}">
<header class="proof-index-head">${kindBadge(node.kind)} <h4>${this.jump(node.detail_key, esc(node.label))}</h4> ${stateBadge(state, `${STATE_LABELS[state]}: ${node.assessment.label}`)}</header>
${node.caption ? `<p class="proof-index-caption">${esc(node.caption)}</p>` : ''}
${node.assessment.explanation ? `<p class="proof-index-text">${esc(node.assessment.explanation)}</p>` : ''}
<p class="proof-index-meta">${reviewNote(node.assessment.independent_review)} <span class="proof-muted">${incoming.get(node.id)} incoming, ${outgoing.get(node.id)} outgoing</span></p>
</article>`;
    });
    const connections = model.connectionList.map((connection) => {
      const state = connection.assessment.state;
      const labels = [];
      if (connection.primary_use_ids.length) labels.push(`${connection.primary_use_ids.length} application${connection.primary_use_ids.length === 1 ? '' : 's'}`);
      if (connection.groups.length) labels.push(`${connection.groups.length} hidden-claim group${connection.groups.length === 1 ? '' : 's'}`);
      return `<article data-proof-index-connection="${esc(connection.id)}" data-edge-from="${esc(connection.from)}" data-edge-to="${esc(connection.to)}" data-edge-state="${esc(state)}" class="proof-index-card s-${esc(state)}">
<header class="proof-index-head"><h4>${this.jump(connection.detail_key, `${esc(this.nodeLabel(connection.from))} <span aria-hidden="true">→</span><span class="proof-visually-hidden"> to </span> ${esc(this.nodeLabel(connection.to))}`)}</h4> ${stateBadge(state, `${STATE_LABELS[state]}: ${connection.assessment.label}`)}</header>
${connection.assessment.explanation ? `<p class="proof-index-text">${esc(connection.assessment.explanation)}</p>` : ''}
<p class="proof-index-meta"><code>${esc(connection.id)}</code> ${labels.length ? `<span class="proof-muted">${esc(labels.join(', '))}</span>` : ''} ${reviewNote(connection.assessment.independent_review)}</p>
</article>`;
    });
    return `<section id="proof-major-index" class="proof-panel" aria-labelledby="proof-index-heading">
<div class="proof-panel-head"><h2 id="proof-index-heading">Major results index</h2><p class="proof-muted">The dependency graph is presented as an index for this build.</p></div>
<div class="proof-index-reasons"><h3>Why an index</h3>${this.model.layout.reasons.length ? `<ul class="proof-reasons">${this.model.layout.reasons.map((reason) => `<li>${esc(reason)}</li>`).join('')}</ul>` : '<p class="proof-muted">No reason was recorded.</p>'}</div>
<h3>Items (${items.length})</h3>
<div class="proof-index-list">${items.join('\n')}</div>
<h3>Connections (${connections.length})</h3>
<div class="proof-index-list">${connections.length ? connections.join('\n') : '<p class="proof-muted">No connections.</p>'}</div>
</section>`;
  }

  // ----- details -----------------------------------------------------------

  detailsHtml() {
    const articles = [...this.model.details.entries()].map(([key, detail]) => this.detailHtml(key, detail));
    return `<section id="proof-details" class="proof-panel" aria-labelledby="proof-details-heading">
<div class="proof-panel-head"><h2 id="proof-details-heading">Details</h2><label class="proof-check"><input id="proof-show-all" type="checkbox"> Show all details</label></div>
<p id="proof-detail-empty" class="proof-muted">Select an item or connection above, or use the search, to open its detail here.</p>
${articles.join('\n')}
</section>`;
  }

  detailHtml(key, detail) {
    const owners = this.owners.get(key) || [];
    const header = owners.length ? owners.map((owner) => (owner.type === 'node' ? this.nodeHeader(owner.node) : this.connectionHeader(owner.connection))).join('\n') : `<div class="proof-detail-owner"><h3 class="proof-detail-heading" tabindex="-1">${esc(key)}</h3></div>`;
    const sections = detail.sections.map((section) => this.sectionHtml(key, section)).join('\n');
    const recordLinks = detail.record_refs.length ? `<p class="proof-detail-records">Records: ${detail.record_refs.map((ref) => this.recordLink(ref, true)).join(', ')}</p>` : '';
    return `<article class="proof-detail" data-proof-detail="${esc(key)}" id="${esc(this.detailId(key))}">
<div class="proof-detail-top"><span class="proof-detail-key"><code>${esc(key)}</code></span><button type="button" class="proof-back">Back to ${this.graph ? 'graph' : 'index'}</button></div>
${header}
${recordLinks}
${sections}
</article>`;
  }

  connectionLine(connection) {
    const state = connection.assessment.state;
    return `<li>${this.jump(connection.detail_key, `<code>${esc(connection.id)}</code>`)} ${esc(this.nodeLabel(connection.from))} <span aria-hidden="true">→</span><span class="proof-visually-hidden"> to </span> ${esc(this.nodeLabel(connection.to))} ${stateBadge(state)}</li>`;
  }

  nodeHeader(node) {
    const incoming = this.model.connectionList.filter((connection) => connection.to === node.id);
    const outgoing = this.model.connectionList.filter((connection) => connection.from === node.id);
    return `<div class="proof-detail-owner proof-owner-node k-${esc(node.kind)}">
<p class="proof-owner-kicker">${kindBadge(node.kind)} <code>${esc(node.id)}</code> <span class="proof-muted">${esc(node.item_ref.collection)}:${esc(node.item_ref.id)}</span>${this.graph ? ` <button type="button" class="proof-focus-node" data-focus-node="${esc(node.id)}">Show in graph</button>` : ''}</p>
<h3 class="proof-detail-heading" tabindex="-1">${esc(node.label)}</h3>
${node.caption ? `<p class="proof-owner-caption">${esc(node.caption)}</p>` : ''}
${this.assessmentBlock(node.assessment)}
<div class="proof-owner-links">
<div><h4>Uses (incoming)</h4>${incoming.length ? `<ul class="proof-plain-list">${incoming.map((connection) => this.connectionLine(connection)).join('')}</ul>` : '<p class="proof-muted">No incoming connections.</p>'}</div>
<div><h4>Used by (outgoing)</h4>${outgoing.length ? `<ul class="proof-plain-list">${outgoing.map((connection) => this.connectionLine(connection)).join('')}</ul>` : '<p class="proof-muted">No outgoing connections.</p>'}</div>
</div>
</div>`;
  }

  connectionHeader(connection) {
    const groups = connection.groups.map((group, i) => `<li class="proof-group">
<p class="proof-group-head"><strong>Group ${i + 1}</strong> ${group.group_id ? this.idLink(group.group_id, 'groups') : '<span class="proof-muted">no group id</span>'} ${group.argument_id ? `<span class="proof-muted">argument</span> ${this.idLink(group.argument_id, 'arguments')}` : ''}</p>
${group.use_ids.length ? `<p class="proof-assessment-meta">Uses: ${group.use_ids.map((id) => this.idLink(id, 'uses')).join(', ')}</p>` : ''}
${group.obligation_ids.length ? `<p class="proof-assessment-meta">Obligations: ${group.obligation_ids.map((id) => `<code>${esc(id)}</code>`).join(', ')}</p>` : ''}
${this.assessmentBlock(group.assessment)}
</li>`);
    return `<div class="proof-detail-owner proof-owner-connection">
<p class="proof-owner-kicker"><span class="proof-pill">Connection</span> <code>${esc(connection.id)}</code></p>
<h3 class="proof-detail-heading" tabindex="-1">${this.jump(this.model.nodes.get(connection.from).detail_key, esc(this.nodeLabel(connection.from)))} <span aria-hidden="true">→</span><span class="proof-visually-hidden"> to </span> ${this.jump(this.model.nodes.get(connection.to).detail_key, esc(this.nodeLabel(connection.to)))}</h3>
${this.assessmentBlock(connection.assessment)}
<dl class="proof-kv">
<dt>Primary applications</dt><dd>${connection.primary_use_ids.length ? connection.primary_use_ids.map((id) => this.idLink(id, 'uses')).join(', ') : '<span class="proof-muted">none</span>'}</dd>
<dt>Support</dt><dd>${connection.support_refs.length ? connection.support_refs.map((ref) => this.recordLink(ref, false)).join(', ') : '<span class="proof-muted">none</span>'}</dd>
<dt>Context</dt><dd>${connection.context_refs.length ? connection.context_refs.map((ref) => this.recordLink(ref, false)).join(', ') : '<span class="proof-muted">none</span>'}</dd>
<dt>Obligations</dt><dd>${connection.obligation_ids.length ? connection.obligation_ids.map((id) => `<code>${esc(id)}</code>`).join(', ') : '<span class="proof-muted">none</span>'}</dd>
</dl>
${groups.length ? `<h4>Hidden-claim groups (${groups.length})</h4><ul class="proof-group-list">${groups.join('')}</ul>` : ''}
</div>`;
  }

  sectionHtml(detailKey, section) {
    const records = section.record_refs.map((ref) => this.recordHtml(this.model.records.get(refKey(ref))));
    const obligations = section.obligation_ids.map((id) => this.obligationHtml(this.model.obligations.get(id)));
    const empty = !records.length && !obligations.length ? '<p class="proof-muted">Nothing recorded in this section.</p>' : '';
    return `<section class="proof-section proof-section-${esc(section.kind)}" data-proof-section="${esc(section.key)}" data-proof-section-kind="${esc(section.kind)}" tabindex="-1">
<h4 class="proof-section-title">${esc(section.title)} <span class="proof-section-kind">${esc(section.kind)}</span></h4>
${section.note ? `<p class="proof-section-note">${esc(section.note)}</p>` : ''}
${records.join('\n')}
${obligations.join('\n')}
${empty}
</section>`;
  }

  obligationHtml(obligation) {
    const state = obligation.assessment.state;
    return `<div class="proof-obligation s-${esc(state)}" data-obligation-id="${esc(obligation.id)}" tabindex="-1">
<p class="proof-record-head"><span class="proof-pill">Obligation</span> <code>${esc(obligation.id)}</code> <span class="proof-pill">${esc(obligation.kind)}</span> <span class="proof-pill">${esc(obligation.role)}</span> <span class="proof-muted">target</span> ${this.recordLink(obligation.target, false)}</p>
${this.assessmentBlock(obligation.assessment)}
${obligation.check_refs.length ? `<p class="proof-assessment-meta">Recorded checks: ${obligation.check_refs.map((ref) => this.recordLink(ref, true)).join(', ')}</p>` : ''}
</div>`;
  }

  recordHtml(record) {
    const key = refKey(record.ref);
    const fragments = this.model.fragments.get(key) || {};
    const body = record.body;
    const collection = record.ref.collection;
    const used = new Set();
    const headline = this.recordHeadline(collection, body, used);
    const rows = [];
    for (const [field, value] of Object.entries(body)) {
      if (used.has(field)) continue;
      rows.push(this.fieldHtml(collection, field, value, fragments));
    }
    for (const [fragmentKey, fragment] of Object.entries(fragments)) {
      const field = Object.keys(FRAGMENT_FIELDS).find((name) => FRAGMENT_FIELDS[name] === fragmentKey);
      if (field && !Object.hasOwn(body, field)) rows.push(this.fragmentRow(field, fragment));
    }
    return `<div class="proof-record proof-record-${esc(collection)}" data-record-ref="${esc(key)}" tabindex="-1">
<p class="proof-record-head"><span class="proof-pill">${esc(collection)}</span> <code>${esc(record.ref.id)}</code> <span class="proof-muted">v${record.ref.version}</span></p>
${headline}
<dl class="proof-record-fields">${rows.join('')}</dl>
</div>`;
  }

  recordHeadline(collection, body, used) {
    const take = (field) => { used.add(field); return body[field]; };
    const text = (value) => (isString(value) ? esc(value) : '');
    switch (collection) {
      case 'items': {
        const label = take('label'), kind = take('kind');
        return `<p class="proof-record-title">${isString(kind) && Object.hasOwn(KINDS, kind) ? kindBadge(kind) : text(kind)} <strong>${text(label)}</strong></p>`;
      }
      case 'uses': {
        const from = take('from'), to = take('to'), type = take('type');
        const link = (ref) => (isObject(ref) ? this.recordLink(ref, false) : isString(ref) ? this.idLink(ref, 'items') : this.valueHtml(ref, 1));
        return `<p class="proof-record-title">${link(from)} <span aria-hidden="true">→</span><span class="proof-visually-hidden"> to </span> ${link(to)} ${isString(type) ? `<span class="proof-pill">${esc(type)}</span>` : ''}</p>`;
      }
      case 'checks': {
        const kind = take('kind'), role = take('role'), state = take('state'), outcome = take('outcome');
        return `<p class="proof-record-title">${[kind, role, state, outcome].filter(isString).map((value) => `<span class="proof-pill">${esc(value)}</span>`).join(' ')}</p>`;
      }
      case 'findings':
      case 'source_issues': {
        const category = take('category'), lifecycle = take('lifecycle');
        return `<p class="proof-record-title">${[category, lifecycle].filter(isString).map((value) => `<span class="proof-pill">${esc(value)}</span>`).join(' ')}</p>`;
      }
      case 'arguments': {
        const label = take('label');
        return isString(label) ? `<p class="proof-record-title"><strong>${esc(label)}</strong></p>` : '';
      }
      case 'anchors': {
        const source = take('source_id'), locator = take('locator');
        const parts = [];
        if (isString(source)) parts.push(this.idLink(source, 'sources'));
        if (isObject(locator)) {
          if (isString(locator.label)) parts.push(`<span class="proof-pill">${esc(locator.label)}</span>`);
          if (locator.page !== undefined && locator.page !== null) parts.push(`<span class="proof-muted">page ${esc(String(locator.page))}</span>`);
          if (locator.start_line !== undefined && locator.start_line !== null) parts.push(`<span class="proof-muted">lines ${esc(String(locator.start_line))}–${esc(String(locator.end_line ?? locator.start_line))}</span>`);
        }
        return parts.length ? `<p class="proof-record-title">${parts.join(' ')}</p>` : '';
      }
      default:
        return '';
    }
  }

  fragmentRow(field, fragment) {
    const label = humanize(field);
    if (Array.isArray(fragment)) {
      return `<dt>${esc(label)}</dt><dd>${fragment.length ? `<ol class="proof-fragment-list">${fragment.map((entry) => `<li class="proof-fragment">${prepareFragment(entry)}</li>`).join('')}</ol>` : '<span class="proof-muted">none</span>'}</dd>`;
    }
    return `<dt>${esc(label)}</dt><dd><div class="proof-fragment">${prepareFragment(fragment)}</div></dd>`;
  }

  fieldHtml(collection, field, value, fragments) {
    const fragmentKey = FRAGMENT_FIELDS[field];
    if (fragmentKey && Object.hasOwn(fragments, fragmentKey)) {
      if (field === 'statement' && isObject(value)) {
        return `<dt>statement</dt><dd>${isString(value.form) ? `<span class="proof-pill">${esc(value.form)}</span> ` : ''}<div class="proof-fragment">${prepareFragment(fragments[fragmentKey])}</div></dd>`;
      }
      return this.fragmentRow(field, fragments[fragmentKey]);
    }
    if (collection === 'anchors' && field === 'excerpt' && isString(value)) {
      return `<dt>excerpt</dt><dd><pre class="proof-excerpt">${esc(value)}</pre></dd>`;
    }
    if (field === 'statement' && isObject(value)) {
      return `<dt>statement</dt><dd>${isString(value.form) ? `<span class="proof-pill">${esc(value.form)}</span> ` : ''}${isString(value.text) ? `<p class="proof-text proof-statement-text">${esc(value.text)}</p>` : this.valueHtml(value, 1)}</dd>`;
    }
    return `<dt>${esc(humanize(field))}</dt><dd>${this.valueHtml(value, 1)}</dd>`;
  }

  valueHtml(value, depth) {
    if (value === null || value === undefined) return '<span class="proof-muted">none</span>';
    if (typeof value === 'boolean' || typeof value === 'number') return `<code>${esc(String(value))}</code>`;
    if (isString(value)) {
      if (!value) return '<span class="proof-muted">empty</span>';
      return value.length > 120 || value.includes('\n') ? `<p class="proof-text">${esc(value)}</p>` : `<span class="proof-text">${esc(value)}</span>`;
    }
    if (depth > 6) return `<code>${esc(JSON.stringify(value))}</code>`;
    if (Array.isArray(value)) {
      if (!value.length) return '<span class="proof-muted">none</span>';
      return `<ul class="proof-value-list">${value.map((entry) => `<li>${this.valueHtml(entry, depth + 1)}</li>`).join('')}</ul>`;
    }
    if (isString(value.collection) && isString(value.id) && Object.keys(value).every((key) => ['collection', 'id', 'version'].includes(key))) {
      return Number.isInteger(value.version) ? this.recordLink(value, true) : this.recordLink({ collection: value.collection, id: value.id }, false);
    }
    const entries = Object.entries(value);
    if (!entries.length) return '<span class="proof-muted">empty</span>';
    return `<dl class="proof-record-fields proof-nested">${entries.map(([key, entry]) => `<dt>${esc(humanize(key))}</dt><dd>${this.valueHtml(entry, depth + 1)}</dd>`).join('')}</dl>`;
  }

  // ----- page --------------------------------------------------------------

  buildLine() {
    const { build, projection } = this.model;
    const parts = [
      `Revision ${build.revision}`,
      `core ${build.core_version}`,
      `${build.kind} build`,
      `built ${build.built_at}`,
      build.audit_id ? `audit ${build.audit_id}` : 'no audit',
      `snapshot revision ${projection.snapshot_revision}`,
      `source identity ${build.source_identity}`,
    ];
    return parts.map((part) => `<span>${esc(part)}</span>`).join('<span class="proof-dot" aria-hidden="true"> · </span>');
  }

  page(fontStyle) {
    const { model } = this;
    const requested = model.summary.scope.target_refs.filter((ref) => ref.collection === 'items').map((ref) => ref.id);
    const beforeGraph = `<div id="proof-main" class="proof-reader"><p class="proof-kicker">Proof audit${model.build.kind === 'release' ? ' · release' : ' · working copy'}</p><details class="proof-build-disclosure"><summary>Audit progress, findings and source version</summary><p class="proof-build">${this.buildLine()}</p>${this.summaryHtml()}</details></div>
${mainNavigation(model.nodeList, model.connectionList, requested)}
<h2 id="proof-graph-heading" class="proof-visually-hidden">Dependency graph</h2><p id="proof-graph-desc" class="proof-visually-hidden">Major results and their recorded uses. Connection colors describe the represented assessments under declared premises.</p>`;
    const afterGraph = `<div class="proof-reader">${this.graph ? this.legendHtml() : this.indexHtml()}
${model.projection.worklist ? '<section id="proof-work-panel" class="proof-panel" aria-labelledby="proof-work-heading"><h2 id="proof-work-heading">Work remaining</h2><p>These are examination states, separate from the proof support colors.</p><p id="proof-work-status" role="status"></p><ol id="proof-work-list"></ol><button id="proof-work-more" type="button">Show more</button><ul id="proof-work-actions"></ul></section>' : ''}
<section id="proof-search-panel" class="proof-panel" aria-labelledby="proof-search-heading">
<h2 id="proof-search-heading">Search the audit record</h2>
<div class="proof-search-row"><label for="proof-search">Search every record body in this projection</label><input id="proof-search" type="search" autocomplete="off" spellcheck="false" placeholder="Search records, e.g. lemma or an identifier"></div>
<p id="proof-search-status" class="proof-muted" role="status" aria-live="polite">Search runs entirely in this page over the embedded projection.</p>
<ol id="proof-search-results" class="proof-search-results" aria-label="Search results"></ol>
</section>
${this.detailsHtml()}
<footer class="proof-footer">
<p>Archify viewer (MIT); JetBrains Mono under the SIL Open Font License. Audit evidence and assessments are drawn from the saved paper records.</p>
</footer></div>`;
    const data = `
<script id="proof-projection" type="application/json">${jsonForScript(model.projection)}</script>
<script id="proof-render-input" type="application/json">${jsonForScript({ title: model.title, build: model.build })}</script>
`;
    // An empty, hidden camera lets the unchanged native shell initialize in index mode.
    // It has no identity-bearing elements and is never presented as a graph.
    const svg = this.graph ? this.svgHtml() : '<svg class="proof-index-placeholder" viewBox="0 0 100 100" aria-hidden="true" xmlns="http://www.w3.org/2000/svg"></svg>';
    return archifyPage({ templatePath: TEMPLATE_PATH, title: model.title, svg, beforeGraph, afterGraph,
      css: PAGE_CSS, runtime: RUNTIME_JS, data, fontStyle, index: !this.graph });
  }
}

// ---------------------------------------------------------------------------
// Fonts (vendored archify template)
// ---------------------------------------------------------------------------

export function loadFontStyle(templatePath = TEMPLATE_PATH) {
  let template;
  try {
    template = readFileSync(templatePath, 'utf8');
  } catch (error) {
    fail('assets', `Cannot read vendored template ${templatePath}: ${error.message}`);
  }
  const matches = [...template.matchAll(/<style id="archify-fonts">[\s\S]*?<\/style>/g)];
  if (matches.length !== 1) fail('assets', 'vendored template changed: expected exactly one <style id="archify-fonts"> block');
  const block = matches[0][0];
  if (!block.includes('@font-face')) fail('assets', 'vendored template changed: font block has no @font-face rule');
  const urls = [...block.matchAll(/url\(\s*["']?([^"')]{0,40})/g)].map((match) => match[1]);
  if (!urls.length || urls.some((url) => !url.startsWith('data:font/woff2;base64,'))) fail('assets', 'vendored template changed: font block references a non-data URL');
  if (/<\/?script/i.test(block)) fail('assets', 'vendored template changed: font block contains script markup');
  return block;
}

// ---------------------------------------------------------------------------
// Verification of the emitted artifact
// ---------------------------------------------------------------------------

function parsePathVertices(d) {
  const vertices = [];
  for (const match of String(d).matchAll(/([MLQHV])\s*([-0-9.\s]+)/g)) {
    const numbers = match[2].trim().split(/\s+/).map(Number);
    if (match[1] === 'M' || match[1] === 'L') vertices.push({ x: numbers[0], y: numbers[1], straight: match[1] === 'L' });
    else if (match[1] === 'Q') vertices.push({ x: numbers[2], y: numbers[3], straight: false });
    else return null;
  }
  return vertices;
}

const classList = (element) => String(element.attrs.class || '').split(/\s+/).filter(Boolean);

export function geometryReceipt(model, scan) {
  const checks = ['finite_node_geometry', 'node_clipping', 'node_overlaps', 'finite_route_geometry', 'edge_endpoints_on_boxes', 'routes_through_unrelated_nodes'];
  if (model.layout.mode !== 'dag') return { status: 'not_applicable', checks: [], diagnostics: [] };
  const diagnostics = [];
  const svg = scan.elements.find((element) => element.tag === 'svg' && classList(element).includes('proof-svg'));
  if (!svg) return { status: 'fail', checks, diagnostics: ['no svg.proof-svg element was emitted'] };
  const viewBox = String(svg.attrs.viewbox || '').split(/\s+/).map(Number);
  const [, , width, height] = viewBox;
  if (viewBox.length !== 4 || !viewBox.every(Number.isFinite)) diagnostics.push('svg viewBox is not four finite numbers');
  const boxes = new Map();
  for (const element of descendants(svg)) {
    if (!hasAttr(element, 'data-node-id')) continue;
    const id = element.attrs['data-node-id'];
    const rect = element.children.find((child) => child.tag === 'rect' && classList(child).includes('proof-node-box'));
    if (!rect) { diagnostics.push(`node ${id} has no proof-node-box rect`); continue; }
    const box = { x: Number(rect.attrs.x), y: Number(rect.attrs.y), w: Number(rect.attrs.width), h: Number(rect.attrs.height) };
    if (![box.x, box.y, box.w, box.h].every(Number.isFinite) || box.w <= 0 || box.h <= 0) { diagnostics.push(`node ${id} has non-finite or empty geometry`); continue; }
    if (box.x < 0 || box.y < 0 || box.x + box.w > width || box.y + box.h > height) diagnostics.push(`node ${id} extends beyond the viewBox`);
    boxes.set(id, box);
  }
  const ids = [...boxes.keys()];
  for (let i = 0; i < ids.length; i += 1) {
    for (let j = i + 1; j < ids.length; j += 1) {
      const a = boxes.get(ids[i]), b = boxes.get(ids[j]);
      if (a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y) diagnostics.push(`node boxes ${ids[i]} and ${ids[j]} overlap`);
    }
  }
  const eps = 0.011;
  for (const element of descendants(svg)) {
    if (!(element.tag === 'path' && hasAttr(element, 'data-edge-id'))) continue;
    const id = element.attrs['data-edge-id'];
    const vertices = parsePathVertices(element.attrs.d);
    if (!vertices || vertices.length < 2 || vertices.some((vertex) => !Number.isFinite(vertex.x) || !Number.isFinite(vertex.y))) { diagnostics.push(`edge ${id} has a non-finite or unparseable route`); continue; }
    const from = boxes.get(element.attrs['data-edge-from']), to = boxes.get(element.attrs['data-edge-to']);
    if (!from || !to) { diagnostics.push(`edge ${id} refers to a node without a box`); continue; }
    const first = vertices[0], last = vertices[vertices.length - 1];
    if (Math.abs(first.x - (from.x + from.w)) > eps || first.y < from.y - eps || first.y > from.y + from.h + eps) diagnostics.push(`edge ${id} does not start on the right edge of node ${element.attrs['data-edge-from']}`);
    if (Math.abs(last.x - to.x) > eps || last.y < to.y - eps || last.y > to.y + to.h + eps) diagnostics.push(`edge ${id} does not end on the left edge of node ${element.attrs['data-edge-to']}`);
    for (let i = 1; i < vertices.length; i += 1) {
      if (!vertices[i].straight) continue;
      const x1 = vertices[i - 1].x, y1 = vertices[i - 1].y, x2 = vertices[i].x, y2 = vertices[i].y;
      for (const [nodeId, box] of boxes) {
        if (nodeId === element.attrs['data-edge-from'] || nodeId === element.attrs['data-edge-to']) continue;
        const horizontal = Math.abs(y1 - y2) < eps && y1 > box.y && y1 < box.y + box.h && Math.max(x1, x2) > box.x && Math.min(x1, x2) < box.x + box.w;
        const vertical = Math.abs(x1 - x2) < eps && x1 > box.x && x1 < box.x + box.w && Math.max(y1, y2) > box.y && Math.min(y1, y2) < box.y + box.h;
        if (horizontal || vertical) diagnostics.push(`edge ${id} crosses unrelated node ${nodeId}`);
      }
    }
  }
  return { status: diagnostics.length ? 'fail' : 'pass', checks, diagnostics };
}

export function representationReceipt(model, scan) {
  const diagnostics = [];
  const checks = [];
  const check = (name, condition, message) => { checks.push(name); if (!condition) diagnostics.push(message); };
  const { projection } = model;
  const all = scan.elements;
  const byAttr = (name) => all.filter((element) => hasAttr(element, name));

  const projectionScripts = all.filter((element) => element.tag === 'script' && element.attrs.id === 'proof-projection');
  let embedded = null;
  try { embedded = projectionScripts.length === 1 ? JSON.parse(projectionScripts[0].text) : null; } catch { embedded = null; }
  check('projection_script', projectionScripts.length === 1 && projectionScripts[0].attrs.type === 'application/json' && embedded !== null && JSON.stringify(embedded) === JSON.stringify(projection) && !/[<>&]/.test(projectionScripts[0].text), 'script#proof-projection is missing, duplicated, or does not round-trip the projection verbatim');
  const inputScripts = all.filter((element) => element.tag === 'script' && element.attrs.id === 'proof-render-input');
  let renderInput = null;
  try { renderInput = inputScripts.length === 1 ? JSON.parse(inputScripts[0].text) : null; } catch { renderInput = null; }
  check('render_input_script', inputScripts.length === 1 && inputScripts[0].attrs.type === 'application/json' && renderInput !== null && JSON.stringify(renderInput) === JSON.stringify({ title: model.title, build: model.build }) && !/[<>&]/.test(inputScripts[0].text), 'script#proof-render-input is missing, duplicated, or does not hold {title, build}');
  const nativeControls = ['btn-theme', 'btn-present', 'btn-export', 'btn-node-finder', 'btn-overview-map', 'btn-route-probe', 'btn-reach-upstream', 'btn-reach-downstream', 'focus-chip'];
  check('archify_shell', nativeControls.every((id) => all.filter((element) => element.attrs.id === id).length === 1)
    && all.some((element) => element.tag === 'script' && element.text?.includes('Archify.focus =') && element.text.includes('Archify.view =')),
  'the bundled Archify viewer shell or runtime is missing');

  const nodeIds = model.nodeList.map((node) => node.id);
  const connectionIds = model.connectionList.map((connection) => connection.id);
  const nodeElements = byAttr('data-node-id');
  const edgeElements = byAttr('data-edge-id');
  const insideSvg = (element) => { for (let parent = element.parent; parent; parent = parent.parent) if (parent.tag === 'svg') return true; return false; };
  if (model.layout.mode === 'dag') {
    check('dag_nodes', sameMultiset(nodeElements.map((element) => element.attrs['data-node-id']), nodeIds)
      && nodeElements.every((element) => element.tag === 'g' && insideSvg(element) && element.attrs.tabindex === '0' && element.attrs.role === 'button'
        && element.attrs['data-node-state'] === model.nodes.get(element.attrs['data-node-id']).assessment.state
        && element.attrs['data-node-kind'] === model.nodes.get(element.attrs['data-node-id']).kind),
    'svg node groups do not match the projection nodes exactly once with state, kind, tabindex and role');
    check('dag_edges', sameMultiset(edgeElements.map((element) => element.attrs['data-edge-id']), connectionIds)
      && edgeElements.every((element) => element.tag === 'path' && insideSvg(element)
        && element.attrs['data-edge-from'] === model.connections.get(element.attrs['data-edge-id']).from
        && element.attrs['data-edge-to'] === model.connections.get(element.attrs['data-edge-id']).to
        && element.attrs['data-edge-state'] === model.connections.get(element.attrs['data-edge-id']).assessment.state),
    'svg edge paths do not match the projection connections exactly once with from, to and state');
    check('no_index_articles', !byAttr('data-proof-index-item').length && !byAttr('data-proof-index-connection').length && !all.some((element) => element.attrs.id === 'proof-major-index'), 'dag mode must not emit index articles');
  } else {
    check('no_graph_elements', !nodeElements.length && !edgeElements.length, 'index mode must not emit data-node-id or data-edge-id elements');
    const sections = all.filter((element) => element.tag === 'section' && element.attrs.id === 'proof-major-index');
    const section = sections[0];
    const within = section ? [...descendants(section)] : [];
    const itemArticles = byAttr('data-proof-index-item');
    const connectionArticles = byAttr('data-proof-index-connection');
    check('index_section', sections.length === 1, 'section#proof-major-index must appear exactly once');
    check('index_items', sameMultiset(itemArticles.map((element) => element.attrs['data-proof-index-item']), nodeIds)
      && itemArticles.every((element) => element.tag === 'article' && within.includes(element) && element.attrs['data-node-state'] === model.nodes.get(element.attrs['data-proof-index-item']).assessment.state),
    'index item articles do not match the projection nodes exactly once with state');
    check('index_connections', sameMultiset(connectionArticles.map((element) => element.attrs['data-proof-index-connection']), connectionIds)
      && connectionArticles.every((element) => element.tag === 'article' && within.includes(element)
        && element.attrs['data-edge-from'] === model.connections.get(element.attrs['data-proof-index-connection']).from
        && element.attrs['data-edge-to'] === model.connections.get(element.attrs['data-proof-index-connection']).to
        && element.attrs['data-edge-state'] === model.connections.get(element.attrs['data-proof-index-connection']).assessment.state),
    'index connection articles do not match the projection connections exactly once with from, to and state');
    const sectionText = section ? textOf(scan, section) : '';
    check('index_reasons', model.layout.reasons.every((reason) => sectionText.includes(reason)), 'index section does not show every layout reason');
  }

  const detailElements = byAttr('data-proof-detail');
  const detailKeys = [...model.details.keys()];
  check('detail_elements', sameMultiset(detailElements.map((element) => element.attrs['data-proof-detail']), detailKeys), 'data-proof-detail elements do not match the detail keys exactly once');
  let sectionsOk = true, recordsOk = true, obligationsOk = true;
  const recordElementsSeen = new Set(), obligationElementsSeen = new Set();
  for (const element of detailElements) {
    const detail = model.details.get(element.attrs['data-proof-detail']);
    if (!detail) { sectionsOk = false; continue; }
    const sectionElements = [...descendants(element)].filter((child) => hasAttr(child, 'data-proof-section'));
    const expected = detail.sections.map((section) => section.key);
    if (JSON.stringify(sectionElements.map((child) => child.attrs['data-proof-section'])) !== JSON.stringify(expected)) { sectionsOk = false; diagnostics.push(`detail ${element.attrs['data-proof-detail']} sections are ${JSON.stringify(sectionElements.map((child) => child.attrs['data-proof-section']))}, expected ${JSON.stringify(expected)}`); continue; }
    sectionElements.forEach((sectionElement, index) => {
      const section = detail.sections[index];
      const inner = [...descendants(sectionElement)];
      const records = inner.filter((child) => hasAttr(child, 'data-record-ref'));
      const obligations = inner.filter((child) => hasAttr(child, 'data-obligation-id'));
      records.forEach((child) => recordElementsSeen.add(child));
      obligations.forEach((child) => obligationElementsSeen.add(child));
      if (JSON.stringify(records.map((child) => child.attrs['data-record-ref'])) !== JSON.stringify(section.record_refs.map(refKey))) { recordsOk = false; diagnostics.push(`section ${section.key} of detail ${element.attrs['data-proof-detail']} does not list its record refs in order`); }
      if (!sameMultiset(obligations.map((child) => child.attrs['data-obligation-id']), section.obligation_ids)) { obligationsOk = false; diagnostics.push(`section ${section.key} of detail ${element.attrs['data-proof-detail']} does not list its obligation ids exactly once`); }
    });
  }
  check('detail_sections', sectionsOk, 'detail sections are missing or out of order');
  check('section_record_refs', recordsOk && byAttr('data-record-ref').every((element) => recordElementsSeen.has(element)), 'record ref elements do not match section record_refs, or appear outside sections');
  check('section_obligation_ids', obligationsOk && byAttr('data-obligation-id').every((element) => obligationElementsSeen.has(element)), 'obligation elements do not match section obligation_ids, or appear outside sections');

  const tally = (list) => Object.fromEntries(STATES.map((state) => [state, list.filter((entry) => entry.assessment.state === state).length]));
  const nodeTally = tally(model.nodeList), connectionTally = tally(model.connectionList);
  const expectedCounts = {};
  for (const state of STATES) { expectedCounts[`nodes.${state}`] = nodeTally[state]; expectedCounts[`connections.${state}`] = connectionTally[state]; }
  for (const field of ['open', 'resolved', 'superseded']) expectedCounts[`findings.${field}`] = model.summary.findings[field];
  for (const field of PROGRESS_FIELDS) expectedCounts[`progress.${field}`] = model.summary.progress[field];
  const countElements = byAttr('data-proof-count');
  check('summary_counts', sameMultiset(countElements.map((element) => element.attrs['data-proof-count']), COUNT_NAMES)
    && countElements.every((element) => element.tag === 'span' && textOf(scan, element).trim() === String(expectedCounts[element.attrs['data-proof-count']])),
  'summary count spans are missing, duplicated or show the wrong number');
  const processElements = byAttr('data-proof-process-complete');
  check('process_complete', processElements.length === 1 && processElements[0].attrs['data-proof-process-complete'] === String(model.summary.progress.process_complete), 'data-proof-process-complete must appear exactly once with the summary value');
  check('summary_findings', sameMultiset(byAttr('data-proof-finding').map((element) => element.attrs['data-proof-finding']), model.summary.findings.refs), 'data-proof-finding elements do not match summary.findings.refs exactly once');
  check('summary_source_limits', sameMultiset(byAttr('data-proof-source-limit').map((element) => element.attrs['data-proof-source-limit']), model.summary.source_limits), 'data-proof-source-limit elements do not match summary.source_limits exactly once');
  check('summary_limitations', sameMultiset(byAttr('data-proof-limitation').map((element) => textOf(scan, element)), model.summary.limitations || []), 'visible completion limitations do not match summary.limitations');
  const searchInputs = all.filter((element) => element.tag === 'input' && element.attrs.id === 'proof-search');
  check('search_input', searchInputs.length === 1, 'input#proof-search must appear exactly once');

  const external = (value) => /^(?:https?:)?\/\//i.test(String(value).trim());
  const externalRequests = all.filter((element) => (hasAttr(element, 'src') && external(element.attrs.src)) || (element.tag === 'link' && hasAttr(element, 'href')) || (element.tag === 'iframe') || (element.tag === 'object') || (element.tag === 'embed') || (element.tag === 'use' && hasAttr(element, 'href') && external(element.attrs.href)));
  const styleUrls = all.filter((element) => element.tag === 'style').flatMap((element) => [...element.text.matchAll(/url\(\s*["']?([^"')]+)/g)].map((match) => match[1].trim()));
  const inlineStyleUrls = all.filter((element) => hasAttr(element, 'style')).flatMap((element) => [...element.attrs.style.matchAll(/url\(\s*["']?([^"')]+)/g)].map((match) => match[1].trim()));
  const badUrls = [...styleUrls, ...inlineStyleUrls].filter((url) => !(url.startsWith('data:') || url.startsWith('#')));
  check('no_external_requests', !externalRequests.length && !badUrls.length, `the page would request external resources: ${[...externalRequests.map((element) => `<${element.tag}>`), ...badUrls].slice(0, 5).join(', ')}`);

  return { status: diagnostics.length ? 'fail' : 'pass', checks, diagnostics };
}

// ---------------------------------------------------------------------------
// Entry points
// ---------------------------------------------------------------------------

export function renderProjection(inputBytes, options = {}) {
  const input = parseInput(inputBytes);
  const model = validateInput(input);
  const graph = model.layout.mode === 'dag' ? layoutGraph(model) : null;
  const fontStyle = options.fontStyle === undefined ? loadFontStyle() : options.fontStyle;
  const renderer = new Renderer(model, graph);
  const html = renderer.page(fontStyle);
  const scan = scanHtml(html);
  const geometry = geometryReceipt(model, scan);
  if (geometry.status === 'fail') fail('verify', 'Geometry verification of the rendered graph failed.', geometry.diagnostics);
  const representation = representationReceipt(model, scan);
  if (representation.status === 'fail') fail('verify', 'Representation verification of the rendered page failed.', representation.diagnostics);
  const bytes = Buffer.from(html, 'utf8');
  const receipt = {
    input_sha256: sha256(inputBytes),
    artifact_sha256: sha256(bytes),
    layout_mode: model.layout.mode,
    nodes: model.nodeList.length,
    connections: model.connectionList.length,
    geometry: { status: geometry.status, checks: geometry.checks, diagnostics: geometry.diagnostics },
    representation: { status: representation.status, checks: representation.checks },
  };
  return { html, bytes, receipt, model };
}

export function main(argv) {
  if (argv.length !== 2) {
    process.stderr.write(JSON.stringify({ ok: false, stage: 'usage', error: 'usage: node render_projection.mjs INPUT.json OUTPUT.html', diagnostics: [] }) + '\n');
    return 2;
  }
  const inputPath = path.resolve(argv[0]);
  const outputPath = path.resolve(argv[1]);
  let stage = 'read';
  const temporary = `${outputPath}.${process.pid}.tmp`;
  let wroteTemporary = false;
  try {
    let inputBytes;
    try {
      inputBytes = readFileSync(inputPath);
    } catch (error) {
      fail('read', `Cannot read input ${inputPath}: ${error.message}`);
    }
    stage = 'render';
    const { bytes, receipt } = renderProjection(inputBytes);
    stage = 'write';
    if (existsSync(temporary)) fail('write', `Temporary file ${temporary} already exists.`);
    writeFileSync(temporary, bytes, { flag: 'wx' });
    wroteTemporary = true;
    renameSync(temporary, outputPath);
    wroteTemporary = false;
    process.stdout.write(JSON.stringify(receipt) + '\n');
    return 0;
  } catch (error) {
    const isRenderError = error instanceof RenderError;
    process.stderr.write(JSON.stringify({ ok: false, stage: isRenderError ? error.stage : stage, error: isRenderError ? error.message : `Unexpected failure: ${error && error.stack ? error.stack : String(error)}`, diagnostics: isRenderError ? error.diagnostics : [] }) + '\n');
    return 1;
  } finally {
    if (wroteTemporary) {
      try { unlinkSync(temporary); } catch { /* nothing left to clean */ }
    }
  }
}

// ---------------------------------------------------------------------------
// Page assets (inline CSS and runtime script)
// ---------------------------------------------------------------------------

const LIGHT_VARS = {
  bg: '#f4f6f9', panel: '#ffffff', ink: '#111827', muted: '#5b6472', line: '#d5dbe3', 'line-strong': '#9aa5b4', grid: '#e6eaf0', accent: '#2563eb', focus: '#d97706',
  'state-green': '#15803d', 'state-red': '#b91c1c', 'state-gray': '#6b7280', 'state-amber': '#b45309',
  'state-green-soft': '#dcfce7', 'state-red-soft': '#fee2e2', 'state-gray-soft': '#e5e7eb', 'state-amber-soft': '#fef3c7',
  ...Object.fromEntries(Object.entries(KINDS).map(([kind, [, , light]]) => [`kind-${kind}`, light])),
};
const DARK_VARS = {
  bg: '#0f141b', panel: '#171e27', ink: '#e6e9ee', muted: '#9aa5b4', line: '#2a3441', 'line-strong': '#4b5766', grid: '#1b2430', accent: '#60a5fa', focus: '#fbbf24',
  'state-green': '#4ade80', 'state-red': '#f87171', 'state-gray': '#9ca3af', 'state-amber': '#fbbf24',
  'state-green-soft': '#14532d', 'state-red-soft': '#7f1d1d', 'state-gray-soft': '#374151', 'state-amber-soft': '#78350f',
  ...Object.fromEntries(Object.entries(KINDS).map(([kind, [, dark]]) => [`kind-${kind}`, dark])),
};
const cssVars = (vars) => Object.entries(vars).map(([name, value]) => `--${name}:${value};`).join('');

const PAGE_CSS = `.proof-reader,.proof-svg,.proof-main-navigation{color-scheme:light;${cssVars(LIGHT_VARS)}--mono:'JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,Consolas,'DejaVu Sans Mono','Liberation Mono',monospace;--serif:Georgia,'Times New Roman',serif;--sans:system-ui,-apple-system,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;}
[data-theme="dark"] .proof-reader,[data-theme="dark"] .proof-svg,[data-theme="dark"].proof-svg,[data-theme="dark"] .proof-main-navigation{color-scheme:dark;${cssVars(DARK_VARS)}}
.proof-reader,.proof-reader *{box-sizing:border-box}
.proof-reader{color:var(--ink);-webkit-text-size-adjust:100%}
.proof-reader a{color:var(--accent)}
.proof-reader code,.proof-reader pre,.proof-reader kbd{font-family:var(--mono);font-size:.92em}
.proof-reader h2,.proof-reader h3,.proof-reader h4{line-height:1.25;margin:0 0 .4em;font-weight:650}
.proof-reader h2{font-size:1.2rem}.proof-reader h3{font-size:1rem}.proof-reader h4{font-size:.95rem}
.proof-reader p{margin:.3em 0}
.proof-reader ul,.proof-reader ol{margin:.3em 0;padding-left:1.3em}
.proof-reader button{font:inherit;color:var(--ink);background:var(--panel);border:1px solid var(--line-strong);border-radius:6px;padding:4px 10px;cursor:pointer}
.proof-reader button:hover{border-color:var(--accent)}
.proof-reader button:focus-visible,.proof-reader a:focus-visible,.proof-reader input:focus-visible,.proof-reader [tabindex]:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.proof-skip{position:absolute;left:-999px;top:8px;background:var(--panel);padding:6px 10px;border:1px solid var(--line-strong);border-radius:6px;z-index:10}
.proof-skip:focus{left:8px}
.proof-visually-hidden{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.proof-header{max-width:1400px;margin:0 auto;padding:20px 0 12px;display:flex;flex-wrap:wrap;gap:12px 24px;align-items:flex-start;justify-content:space-between}
.proof-header-text{min-width:0;flex:1 1 320px}
.proof-kicker{margin:0;font-family:var(--mono);font-size:.78rem;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.proof-build{margin:.2em 0 0;font-family:var(--mono);font-size:.78rem;color:var(--muted);overflow-wrap:anywhere}
.proof-toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.proof-panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;min-width:0}
.proof-panel-head{display:flex;flex-wrap:wrap;gap:6px 16px;align-items:baseline;justify-content:space-between}
.proof-panel-head p{flex:1 1 300px;margin:0}
.proof-muted{color:var(--muted)}
.proof-dot{color:var(--muted)}
.proof-pill{display:inline-block;font-family:var(--mono);font-size:.74rem;line-height:1.4;padding:0 6px;border:1px solid var(--line-strong);border-radius:999px;color:var(--muted);vertical-align:middle;overflow-wrap:anywhere}
.proof-summary-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}
.proof-card{border:1px solid var(--line);border-radius:10px;padding:12px;min-width:0}
.proof-kv{display:grid;grid-template-columns:minmax(120px,max-content) 1fr;gap:3px 12px;margin:.3em 0}
.proof-kv dt{color:var(--muted)}
.proof-kv dd{margin:0;min-width:0;overflow-wrap:anywhere}
.proof-kv-inline{grid-template-columns:repeat(3,auto);justify-content:start;gap:2px 18px}
.proof-kv-inline dt{grid-row:1}.proof-kv-inline dd{grid-row:2;font-size:1.3rem;font-weight:650}
.proof-count{font-variant-numeric:tabular-nums;font-weight:650}
.proof-process{font-weight:650;padding:6px 10px;border-radius:8px;display:inline-block}
.proof-process.is-complete{background:var(--state-green-soft);color:var(--state-green)}
.proof-process.is-incomplete{background:var(--state-amber-soft);color:var(--state-amber)}
.proof-state-list,.proof-plain-list,.proof-finding-list,.proof-legend-list,.proof-group-list,.proof-value-list,.proof-search-results,.proof-fragment-list{list-style:none;padding:0;margin:.3em 0}
.proof-state-list li,.proof-finding-list li,.proof-plain-list li{display:flex;flex-wrap:wrap;gap:4px 8px;align-items:center;padding:2px 0}
.proof-finding-text{color:var(--muted);flex:1 1 100%}
.proof-state-badge{display:inline-flex;align-items:center;gap:4px;font-family:var(--mono);font-size:.76rem;line-height:1.5;padding:0 8px 0 5px;border-radius:999px;border:1px solid currentColor;white-space:nowrap}
.proof-state-glyph{font-weight:700}
.s-green .proof-state-badge,.proof-state-badge.s-green{color:var(--state-green);background:var(--state-green-soft)}
.s-red .proof-state-badge,.proof-state-badge.s-red{color:var(--state-red);background:var(--state-red-soft)}
.s-gray .proof-state-badge,.proof-state-badge.s-gray{color:var(--state-gray);background:var(--state-gray-soft)}
.s-amber .proof-state-badge,.proof-state-badge.s-amber{color:var(--state-amber);background:var(--state-amber-soft)}
.proof-kind-badge{display:inline-block;font-family:var(--mono);font-size:.74rem;line-height:1.5;padding:0 7px;border-radius:4px;color:#fff;background:var(--tone,var(--muted));white-space:nowrap}
${Object.keys(KINDS).map((kind) => `.k-${kind}{--tone:var(--kind-${kind})}`).join('\n')}
.proof-review{font-family:var(--mono);font-size:.76rem;color:var(--muted)}
.proof-review.r-complete{color:var(--state-green)}.proof-review.r-pending{color:var(--state-amber)}.proof-review.r-disputed,.proof-review.r-compromised{color:var(--state-red)}
.proof-ir-sample{font-family:var(--mono);font-size:.72rem;border:1px solid var(--line-strong);border-radius:999px;padding:0 5px}
.proof-reasons{margin:.4em 0;padding-left:1.2em}
.proof-canvas{margin-top:10px;overflow-x:auto;overflow-y:hidden;border:1px solid var(--line);border-radius:10px;background:var(--panel);max-width:100%}
.proof-canvas svg{display:block;max-width:none}
.proof-canvas.is-fit svg{width:100%;height:auto}
.proof-svg text{font-family:var(--mono);fill:var(--ink)}
.proof-grid-line{stroke:var(--grid);fill:none}
.proof-component path{stroke:var(--line-strong);fill:none}
.proof-component text{fill:var(--muted)}
.proof-node{cursor:pointer;outline:none}
.proof-node-box{fill:var(--panel);stroke:var(--tone,var(--muted));stroke-width:1.5}
@supports (fill: color-mix(in srgb, red 10%, white)){.proof-node-box{fill:color-mix(in srgb,var(--tone,var(--muted)) 16%,var(--panel))}}
.proof-node-ring{fill:none;stroke:var(--state,var(--muted));stroke-width:2.5}
.proof-node.s-green{--state:var(--state-green)}.proof-node.s-red{--state:var(--state-red)}.proof-node.s-gray{--state:var(--state-gray)}.proof-node.s-amber{--state:var(--state-amber)}
.proof-node.s-red .proof-node-ring{stroke-width:4}
.proof-node.s-amber .proof-node-ring{stroke-dasharray:7 4}
.proof-node.s-gray .proof-node-ring{stroke-dasharray:2 4}
.proof-node:hover .proof-node-box{stroke-width:3}
.proof-node:focus-visible .proof-node-box{stroke:var(--focus);stroke-width:3.5}
.proof-node[aria-pressed="true"] .proof-node-box{stroke-width:3}
.proof-node-caption{fill:var(--muted)}
.proof-node-state circle{fill:var(--state,var(--muted));stroke:var(--panel);stroke-width:2}
.proof-node-state text{fill:#fff}
.proof-node-review rect{fill:var(--panel);stroke:var(--muted);stroke-width:1.2}
.proof-node-review text{fill:var(--muted);font-weight:700}
.proof-node-review.r-complete rect{stroke:var(--state-green);fill:var(--state-green)}.proof-node-review.r-complete text{fill:#fff}
.proof-node-review.r-pending rect{stroke:var(--state-amber)}.proof-node-review.r-pending text{fill:var(--state-amber)}
.proof-node-review.r-disputed rect{stroke:var(--state-red)}.proof-node-review.r-disputed text{fill:var(--state-red)}
.proof-node-review.r-compromised rect{stroke:var(--state-red);fill:var(--state-red);stroke-dasharray:3 2}.proof-node-review.r-compromised text{fill:#fff}
.proof-edge{fill:none;stroke-width:2;stroke:var(--muted)}
.proof-edge.s-green{stroke:var(--state-green)}.proof-edge.s-red{stroke:var(--state-red);stroke-width:2.6}.proof-edge.s-gray{stroke:var(--state-gray);stroke-dasharray:2 4}.proof-edge.s-amber{stroke:var(--state-amber);stroke-dasharray:7 4}
.proof-edge-hit{fill:none;stroke:transparent;stroke-width:14;pointer-events:stroke;cursor:pointer}
.proof-arrowhead{fill:var(--muted)}
.proof-arrowhead.s-green{fill:var(--state-green)}.proof-arrowhead.s-red{fill:var(--state-red)}.proof-arrowhead.s-gray{fill:var(--state-gray)}.proof-arrowhead.s-amber{fill:var(--state-amber)}
.proof-edge-badge{cursor:pointer;outline:none}
.proof-edge-badge rect{fill:var(--panel);stroke:var(--state,var(--muted));stroke-width:1.2}
.proof-edge-badge.s-green{--state:var(--state-green)}.proof-edge-badge.s-red{--state:var(--state-red)}.proof-edge-badge.s-gray{--state:var(--state-gray)}.proof-edge-badge.s-amber{--state:var(--state-amber)}
.proof-edge-badge text{font-size:10px;fill:var(--ink)}
.proof-edge-badge:hover rect,.proof-edge-badge:focus-visible rect{stroke:var(--focus);stroke-width:2.5}
.proof-badge-leader{stroke:var(--line-strong);stroke-dasharray:2 3;fill:none}
.proof-badge-anchor{fill:var(--line-strong)}
.proof-legend{display:flex;flex-wrap:wrap;gap:8px 24px;margin-top:10px;font-size:.88rem}
.proof-legend-list{display:flex;flex-wrap:wrap;gap:4px 14px}
.proof-legend-list li{display:flex;align-items:center;gap:6px}
.proof-legend-swatch{display:inline-block;width:14px;height:14px;border-radius:4px;background:var(--tone,var(--muted))}
.proof-legend-note{color:var(--muted);flex:1 1 100%;margin:0}
.proof-index-reasons{border-left:3px solid var(--state-amber);padding:4px 12px;margin:8px 0 12px;background:var(--state-amber-soft)}
.proof-index-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:10px;margin-bottom:14px}
.proof-index-card{border:1px solid var(--line);border-left:5px solid var(--state,var(--line-strong));border-radius:10px;padding:10px 12px;min-width:0}
.proof-index-card.s-green{--state:var(--state-green)}.proof-index-card.s-red{--state:var(--state-red)}.proof-index-card.s-gray{--state:var(--state-gray)}.proof-index-card.s-amber{--state:var(--state-amber)}
.proof-index-head{display:flex;flex-wrap:wrap;gap:4px 8px;align-items:center}
.proof-index-head h4{margin:0;flex:1 1 auto;overflow-wrap:anywhere}
.proof-index-caption,.proof-index-text{overflow-wrap:anywhere}
.proof-index-meta{display:flex;flex-wrap:wrap;gap:4px 10px;align-items:center;font-size:.85rem}
.proof-search-row{display:flex;flex-direction:column;gap:4px;max-width:720px}
#proof-search{font:inherit;padding:8px 10px;border:1px solid var(--line-strong);border-radius:8px;background:var(--bg);color:var(--ink);width:100%}
.proof-search-results{display:grid;gap:6px;margin-top:8px}
.proof-search-hit{display:block;width:100%;text-align:left;padding:8px 10px;border-radius:8px}
.proof-search-hit strong{font-family:var(--mono);font-size:.86rem}
.proof-search-where{display:block;color:var(--muted);font-size:.84rem}
.proof-search-snippet{display:block;color:var(--muted);font-size:.86rem;overflow-wrap:anywhere}
.proof-search-snippet mark{background:var(--state-amber-soft);color:inherit}
html[data-js]:not(.show-all-details) [data-proof-detail]:not(.is-active){display:none}
html:not([data-js]) #proof-detail-empty,html.show-all-details #proof-detail-empty,html[data-js] #proof-detail-empty.is-hidden{display:none}
.proof-check{display:inline-flex;align-items:center;gap:6px}
.proof-detail{border:1px solid var(--line);border-radius:10px;padding:14px;margin-top:12px;min-width:0}
.proof-detail-top{display:flex;flex-wrap:wrap;gap:8px;justify-content:space-between;align-items:center;margin-bottom:6px}
.proof-detail-key{font-size:.8rem;color:var(--muted);overflow-wrap:anywhere}
.proof-detail-owner{margin-bottom:12px}
.proof-owner-kicker{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center;margin:0 0 4px}
.proof-detail-heading{outline-offset:4px;overflow-wrap:anywhere}
.proof-owner-caption{color:var(--muted)}
.proof-owner-links{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px;margin-top:8px}
.proof-owner-links h4{margin-bottom:.2em}
.proof-assessment{border-left:4px solid var(--state,var(--line-strong));padding:4px 12px;margin:8px 0;background:var(--bg);border-radius:0 8px 8px 0}
.proof-assessment.s-green{--state:var(--state-green)}.proof-assessment.s-red{--state:var(--state-red)}.proof-assessment.s-gray{--state:var(--state-gray)}.proof-assessment.s-amber{--state:var(--state-amber)}
.proof-assessment-head{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center}
.proof-assessment-text{overflow-wrap:anywhere}
.proof-assessment-meta{font-size:.88rem;color:var(--muted);overflow-wrap:anywhere}
.proof-group{border:1px dashed var(--line-strong);border-radius:8px;padding:6px 10px;margin:6px 0}
.proof-group-head{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center}
.proof-section{border-top:1px solid var(--line);padding-top:10px;margin-top:10px}
.proof-section-title{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:baseline}
.proof-section-kind{font-family:var(--mono);font-size:.74rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.proof-section-note{color:var(--muted);overflow-wrap:anywhere}
.proof-record,.proof-obligation{border:1px solid var(--line);border-radius:8px;padding:8px 12px;margin:8px 0;min-width:0;overflow-wrap:anywhere;background:var(--panel)}
.proof-obligation{border-left:4px solid var(--state,var(--line-strong))}
.proof-obligation.s-green{--state:var(--state-green)}.proof-obligation.s-red{--state:var(--state-red)}.proof-obligation.s-gray{--state:var(--state-gray)}.proof-obligation.s-amber{--state:var(--state-amber)}
.proof-record-head{display:flex;flex-wrap:wrap;gap:6px 8px;align-items:center;margin:0 0 4px;font-size:.9rem}
.proof-record-title{display:flex;flex-wrap:wrap;gap:6px 8px;align-items:center;margin:0 0 6px}
.proof-record-fields{display:grid;grid-template-columns:minmax(110px,max-content) minmax(0,1fr);gap:4px 14px;margin:.3em 0}
.proof-record-fields dt{color:var(--muted);font-family:var(--mono);font-size:.8rem;padding-top:2px}
.proof-record-fields dd{margin:0;min-width:0}
.proof-nested{border-left:2px solid var(--line);padding-left:10px}
.proof-text{white-space:pre-wrap;overflow-wrap:anywhere;margin:0}
.proof-statement-text{font-family:var(--serif);font-size:1.05em}
.proof-fragment{font-family:var(--serif);font-size:1.05em;line-height:1.6;max-width:100%;overflow-wrap:anywhere}
.proof-fragment p{margin:.3em 0}
.proof-fragment-list{counter-reset:condition;padding-left:0}
.proof-fragment-list li{padding:2px 0}
.proof-formula{display:inline-block;max-width:100%;overflow-x:auto;overflow-y:hidden;vertical-align:middle;padding:2px 0}
.proof-formula-block{display:block;margin:.3em 0}
.proof-formula math{max-width:none}
.proof-excerpt{white-space:pre-wrap;overflow-wrap:anywhere;overflow-x:auto;margin:0;padding:8px 10px;background:var(--bg);border-radius:6px;font-size:.84rem;line-height:1.45}
.proof-ref-text{overflow-wrap:anywhere}
.proof-value-list{padding-left:0}
.proof-value-list li{padding:1px 0}
.is-target{outline:2px solid var(--focus);outline-offset:3px}
.proof-footer{max-width:1400px;margin:24px auto 0;color:var(--muted);font-size:.84rem}
@media (max-width:640px){.proof-kv{grid-template-columns:1fr}.proof-kv dt{margin-top:4px}.proof-record-fields{grid-template-columns:1fr}.proof-record-fields dt{margin-top:4px}.proof-kv-inline{grid-template-columns:repeat(3,auto)}}
@media print{.proof-toolbar,#proof-search-panel,.proof-back,.proof-focus-node,.proof-check,#proof-detail-empty{display:none!important}[data-proof-detail]{display:block!important}.proof-canvas{overflow:visible}.proof-canvas svg{width:100%;height:auto}}`;

const RUNTIME_JS = `(function () {
  'use strict';
  var doc = document, root = doc.documentElement;
  var projection = null;
  try { projection = JSON.parse(doc.getElementById('proof-projection').textContent); } catch (error) { projection = null; }
  var svg = doc.querySelector('svg.proof-svg');
  var nodeList = svg ? Array.prototype.slice.call(svg.querySelectorAll('g[data-node-id]')) : [];
  var lastGraphTarget = null;
  var viewer = window.Archify, viewerKey = '';
  var container = doc.querySelector('.diagram-container'), chip = doc.getElementById('focus-chip');
  var detailsPanel = doc.getElementById('proof-details');
  if (svg && chip && container) {
    container.insertAdjacentElement('afterend', chip);
    chip.removeAttribute('data-radar-yielded');chip.removeAttribute('aria-hidden');
    if (detailsPanel) { chip.insertAdjacentElement('afterend', detailsPanel); detailsPanel.classList.add('proof-reader'); }
    var reachLabel = chip.querySelector('.semantic-passport-reach-label');
    if (reachLabel) reachLabel.textContent = 'Trace dependencies';
  }

  function cssEscape(value) {
    if (window.CSS && typeof CSS.escape === 'function') return CSS.escape(value);
    return String(value).replace(/["\\\\]/g, '\\\\$&');
  }
  function query(scope, attribute, value) {
    return scope.querySelector('[' + attribute + '="' + cssEscape(value) + '"]');
  }
  function detailFor(key) { return query(doc, 'data-proof-detail', key); }

  // Details: one at a time unless "show all" is checked.
  var showAll = doc.getElementById('proof-show-all');
  function applyShowAll() { root.classList.toggle('show-all-details', !!(showAll && showAll.checked)); }
  if (showAll) { showAll.addEventListener('change', applyShowAll); applyShowAll(); }

  function clearTargets() {
    var previous = doc.querySelectorAll('.is-target');
    for (var i = 0; i < previous.length; i++) previous[i].classList.remove('is-target');
  }
  function showDetail(key, sectionKey, recordRef, options) {
    var article = detailFor(key);
    if (!article) return false;
    viewerKey = key;
    if (svg && viewer && viewer.focus && !(options && options.fromViewer)) {
      var selected = projection.nodes.find(function (node) { return node.detail_key === key; });
      var connection = projection.connections.find(function (edge) { return edge.detail_key === key; });
      if (selected && viewer.focus.active() !== selected.id) viewer.focus.set(selected.id, { toggle: false });
      else if (connection && (!viewer.focus.relationship() || viewer.focus.relationship().id !== connection.id)) viewer.focus.inspectRelationshipById(connection.id, { toggle: false });
    }
    var active = doc.querySelectorAll('[data-proof-detail].is-active');
    for (var i = 0; i < active.length; i++) active[i].classList.remove('is-active');
    article.classList.add('is-active');
    var empty = doc.getElementById('proof-detail-empty');
    if (empty) empty.classList.add('is-hidden');
    for (i = 0; i < nodeList.length; i++) nodeList[i].setAttribute('aria-pressed', nodeList[i].getAttribute('data-detail-key') === key ? 'true' : 'false');
    var target = article;
    var section = sectionKey ? query(article, 'data-proof-section', sectionKey) : null;
    if (section) target = section;
    if (recordRef) {
      var record = section ? query(section, 'data-record-ref', recordRef) : null;
      if (!record) record = query(article, 'data-record-ref', recordRef);
      if (record) target = record;
    }
    if (options && options.obligationId) {
      var obligation = section ? query(section, 'data-obligation-id', options.obligationId) : null;
      if (!obligation) obligation = query(article, 'data-obligation-id', options.obligationId);
      if (obligation) target = obligation;
    }
    clearTargets();
    if (target !== article) target.classList.add('is-target');
    var focusTarget = target === article ? (article.querySelector('.proof-detail-heading') || article) : target;
    if (!(options && options.silent)) {
      try { target.scrollIntoView({ block: 'start' }); } catch (error) { target.scrollIntoView(); }
      try { focusTarget.focus({ preventScroll: true }); } catch (error) { focusTarget.focus(); }
    }
    return true;
  }

  function nodeById(id) {
    for (var i = 0; i < nodeList.length; i++) if (nodeList[i].getAttribute('data-node-id') === id) return nodeList[i];
    return null;
  }
  function backToGraph() {
    if (viewer && viewer.focus) viewer.focus.clear();
    var target = lastGraphTarget || nodeList[0] || doc.getElementById('proof-major-index');
    if (!target) return;
    try { target.scrollIntoView({ block: 'center' }); } catch (error) { target.scrollIntoView(); }
    if (typeof target.focus === 'function') target.focus();
  }
  function openTarget(element) {
    var key = element.getAttribute('data-detail-key');
    if (!key) return;
    lastGraphTarget = element;
    showDetail(key);
  }

  function readableView(id) {
    if (!svg || !viewer || !viewer.view) return;
    var node = nodeById(id) || nodeList[0];
    if (!node) return;
    var label = node.querySelector('text[data-node-label]'), vb = svg.viewBox.baseVal;
    var size = label ? Number(label.getAttribute('font-size')) || 14 : 14;
    var base = Math.min(svg.clientWidth / vb.width, svg.clientHeight / vb.height);
    var scale = Math.max(1, Math.min(3, Math.ceil(12 / (size * base) * 4) / 4));
    viewer.view.centerAt(num(node, 'data-node-x') + 85, num(node, 'data-node-y') + 32,
      { scale: scale, minimumScale: scale, instant: true });
    doc.getElementById('proof-view-status').textContent = 'Readable view shows part of the full graph. Drag the background or select another result to navigate.';
  }
  function syncViewer() {
    if (!viewer || !viewer.focus || !svg) return;
    var relationship = viewer.focus.relationship(), active = viewer.focus.active();
    var node = projection.nodes.find(function (entry) { return entry.id === active; });
    var edge = relationship && projection.connections.find(function (entry) { return entry.id === relationship.id; });
    var key = edge ? edge.detail_key : node ? node.detail_key : '';
    doc.querySelectorAll('[data-proof-main]').forEach(function (button) { button.setAttribute('aria-pressed', String(button.getAttribute('data-proof-main') === active)); });
    if (key && key !== viewerKey) showDetail(key, null, null, { fromViewer: true });
    if (!key && viewerKey) {
      viewerKey = '';
      doc.querySelectorAll('[data-proof-detail].is-active').forEach(function (article) { article.classList.remove('is-active'); });
      var empty = doc.getElementById('proof-detail-empty');
      if (empty) empty.classList.remove('is-hidden');
      clearTargets();
    }
    svg.querySelectorAll('[data-edge-badge]').forEach(function (badge) {
      var path = query(svg, 'data-edge-id', badge.getAttribute('data-edge-badge'));
      var matched = path && (svg.hasAttribute('data-reach-active') ? path.hasAttribute('data-reach-match') : path.hasAttribute('data-focus-match'));
      if (matched) badge.setAttribute('data-proof-emphasized', ''); else badge.removeAttribute('data-proof-emphasized');
    });
  }
  if (svg && viewer && viewer.focus) {
    new MutationObserver(syncViewer).observe(svg, { subtree: true, attributes: true,
      attributeFilter: ['data-focus-active', 'data-focus-selected', 'data-relationship-pin-active', 'data-reach-active', 'data-reach-match'] });
    doc.getElementById('proof-full-structure').addEventListener('click', function () {
      viewer.focus.clear();
      if (viewer.semanticLens) viewer.semanticLens.clear({ preserveView: true });
      if (viewer.routeProbe) viewer.routeProbe.clear({ restoreFocus: false });
      viewer.view.reset();
      doc.getElementById('proof-view-status').textContent = 'Complete structure fitted to the viewer. Select a result or choose Readable view for larger text.';
    });
    doc.getElementById('proof-readable-view').addEventListener('click', function () { readableView(viewer.focus.active()); });
    // Native background handling must not swallow application badges.
    doc.addEventListener('click', function (event) {
      var badge = event.target.closest && event.target.closest('[data-edge-badge]');
      if (!badge) return;
      event.preventDefault(); event.stopPropagation();
      viewer.focus.inspectRelationshipById(badge.getAttribute('data-edge-badge'), { toggle: false });syncViewer();
    }, true);
    requestAnimationFrame(function () { syncViewer();readableView(viewer.focus.active()); });
  } else {
    // The index uses the same shell, but exporting a hidden empty camera would be misleading.
    var exportButton = doc.getElementById('btn-export');
    if (exportButton) { exportButton.disabled = true; exportButton.title = 'The graph is unavailable for this cyclic projection; use the complete index below.'; }
  }

  doc.addEventListener('click', function (event) {
    var origin = event.target;
    if (!origin || typeof origin.closest !== 'function') return;
    var main = origin.closest('[data-proof-main]');
    if (main) {
      var mainId = main.getAttribute('data-proof-main');
      var mainNode = projection.nodes.find(function (node) { return node.id === mainId; });
      if (mainNode) { showDetail(mainNode.detail_key); readableView(mainId); }
      return;
    }
    var jump = origin.closest('.proof-jump');
    if (jump) {
      var key = jump.getAttribute('data-jump-detail');
      if (key && showDetail(key, jump.getAttribute('data-jump-section'), jump.getAttribute('data-jump-record'))) event.preventDefault();
      return;
    }
    var focusNode = origin.closest('[data-focus-node]');
    if (focusNode) {
      var node = nodeById(focusNode.getAttribute('data-focus-node'));
      if (node) {
        lastGraphTarget = node;
        showDetail(node.getAttribute('data-detail-key'), null, null, { silent: true });
        readableView(node.getAttribute('data-node-id'));
        try { container.scrollIntoView({ block: 'center' }); } catch (error) { container.scrollIntoView(); }
        try { node.focus({ preventScroll: true }); } catch (error) { node.focus(); }
      }
      return;
    }
    if (origin.closest('.proof-back')) backToGraph();
  });
  doc.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && event.target && typeof event.target.closest === 'function' && event.target.closest('#proof-details')) backToGraph();
  });

  function num(element, attribute) { return parseFloat(element.getAttribute(attribute)); }
  function neighbour(element, direction) {
    var id = element.getAttribute('data-node-id');
    var y = num(element, 'data-node-y');
    var best = null, bestDistance = Infinity, i, candidate, distance;
    var edges = svg.querySelectorAll(direction > 0 ? 'path[data-edge-from="' + cssEscape(id) + '"]' : 'path[data-edge-to="' + cssEscape(id) + '"]');
    for (i = 0; i < edges.length; i++) {
      candidate = nodeById(edges[i].getAttribute(direction > 0 ? 'data-edge-to' : 'data-edge-from'));
      if (!candidate) continue;
      distance = Math.abs(num(candidate, 'data-node-y') - y);
      if (distance < bestDistance) { best = candidate; bestDistance = distance; }
    }
    if (best) return best;
    var rank = parseInt(element.getAttribute('data-node-rank'), 10) + direction;
    for (i = 0; i < nodeList.length; i++) {
      candidate = nodeList[i];
      if (parseInt(candidate.getAttribute('data-node-rank'), 10) !== rank) continue;
      distance = Math.abs(num(candidate, 'data-node-y') - y);
      if (distance < bestDistance) { best = candidate; bestDistance = distance; }
    }
    return best;
  }
  function vertical(element, direction) {
    var rank = element.getAttribute('data-node-rank'), y = num(element, 'data-node-y');
    var best = null, bestDistance = Infinity;
    for (var i = 0; i < nodeList.length; i++) {
      var candidate = nodeList[i];
      if (candidate === element || candidate.getAttribute('data-node-rank') !== rank) continue;
      var delta = (num(candidate, 'data-node-y') - y) * direction;
      if (delta > 0 && delta < bestDistance) { best = candidate; bestDistance = delta; }
    }
    return best;
  }
  if (svg) {
    svg.addEventListener('keydown', function (event) {
      var origin = event.target;
      if (!origin || typeof origin.closest !== 'function') return;
      var element = origin.closest('[data-detail-key]');
      if (!element) return;
      if ((event.key === 'Enter' || event.key === ' ') && element.hasAttribute('data-edge-badge')) { event.preventDefault(); openTarget(element); return; }
      if (!element.hasAttribute('data-node-id')) return;
      var next = null;
      if (event.key === 'ArrowRight') next = neighbour(element, 1);
      else if (event.key === 'ArrowLeft') next = neighbour(element, -1);
      else if (event.key === 'ArrowDown') next = vertical(element, 1);
      else if (event.key === 'ArrowUp') next = vertical(element, -1);
      else if (event.key === 'Home') next = nodeList[0];
      else if (event.key === 'End') next = nodeList[nodeList.length - 1];
      else return;
      event.preventDefault();
      if (next) {
        readableView(next.getAttribute('data-node-id'));
        try { next.focus({ preventScroll: true }); } catch (error) { next.focus(); }
      }
    });
  }

  // Full-dataset search over record_locations using the embedded projection.
  var searchInput = doc.getElementById('proof-search'), results = doc.getElementById('proof-search-results'), status = doc.getElementById('proof-search-status');
  function refKey(ref) { return ref.collection + ':' + ref.id + ':' + ref.version; }
  function flatten(value, out) {
    if (value === null || value === undefined) return;
    if (typeof value === 'string') { out.push(value); return; }
    if (typeof value === 'number' || typeof value === 'boolean') { out.push(String(value)); return; }
    if (Array.isArray(value)) { for (var i = 0; i < value.length; i++) flatten(value[i], out); return; }
    for (var key in value) if (Object.prototype.hasOwnProperty.call(value, key)) { out.push(key.split('_').join(' ')); flatten(value[key], out); }
  }
  function recordElement(detailKey, sectionKey, key) {
    var article = detailFor(detailKey);
    if (!article) return null;
    var section = query(article, 'data-proof-section', sectionKey);
    return section ? query(section, 'data-record-ref', key) : null;
  }
  function buildIndex() {
    var entries = [];
    if (!projection || !Array.isArray(projection.record_locations)) return entries;
    var records = {}, owners = {}, titles = {}, seen = {}, i;
    for (i = 0; i < (projection.records || []).length; i++) records[refKey(projection.records[i].ref)] = projection.records[i];
    for (i = 0; i < (projection.nodes || []).length; i++) owners[projection.nodes[i].detail_key] = projection.nodes[i].label;
    for (i = 0; i < (projection.connections || []).length; i++) if (!owners[projection.connections[i].detail_key]) owners[projection.connections[i].detail_key] = 'Connection ' + projection.connections[i].id;
    var detailKeys = Object.keys(projection.details || {});
    for (i = 0; i < detailKeys.length; i++) {
      var sections = projection.details[detailKeys[i]].sections || [];
      for (var s = 0; s < sections.length; s++) titles[detailKeys[i] + '\\u0000' + sections[s].key] = sections[s].title;
    }
    for (i = 0; i < projection.record_locations.length; i++) {
      var location = projection.record_locations[i];
      var key = refKey(location.ref), record = records[key];
      if (!record) continue;
      var dedupe = key + '\\u0000' + location.detail_key + '\\u0000' + location.section_key;
      if (seen[dedupe]) continue;
      seen[dedupe] = true;
      var parts = [key, location.ref.collection];
      flatten(record.body, parts);
      var element = recordElement(location.detail_key, location.section_key, key);
      var text = parts.join(' ') + (element ? ' ' + element.textContent.replace(/\\s+/g, ' ') : '');
      entries.push({ key: key, detail: location.detail_key, section: location.section_key, owner: owners[location.detail_key] || location.detail_key, title: titles[location.detail_key + '\\u0000' + location.section_key] || location.section_key, text: text, lower: text.toLowerCase() });
    }
    var workTasks = projection.worklist ? projection.worklist.tasks : [];
    for (i = 0; i < workTasks.length; i++) {
      var task = workTasks[i], taskLocation = task.location;
      if (!taskLocation || !taskLocation.obligation_id) continue;
      var taskText = [task.id, task.label, task.role, task.state, task.next_action || ''].join(' ');
      entries.push({key:task.id, detail:taskLocation.detail_key, section:taskLocation.section_key,
        owner:owners[taskLocation.detail_key] || 'Audit', title:task.label, text:taskText,
        lower:taskText.toLowerCase(), obligationId:task.id});
    }
    return entries;
  }
  var index = buildIndex();
  function element(tag, className, text) {
    var node = doc.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function runSearch() {
    if (!searchInput || !results || !status) return;
    var raw = (searchInput.value || '').trim().toLowerCase();
    results.textContent = '';
    if (!raw) { status.textContent = index.length ? 'Type to search ' + index.length + ' record locations.' : 'No searchable record locations in this projection.'; return; }
    var terms = raw.split(/\\s+/), matches = [], i, t;
    for (i = 0; i < index.length && matches.length < 40; i++) {
      var entry = index[i], ok = true;
      for (t = 0; t < terms.length; t++) if (entry.lower.indexOf(terms[t]) < 0) { ok = false; break; }
      if (ok) matches.push(entry);
    }
    status.textContent = matches.length ? matches.length + (matches.length === 40 ? '+' : '') + ' matching record location' + (matches.length === 1 ? '' : 's') : 'No matches.';
    for (i = 0; i < matches.length; i++) {
      (function (entry) {
        var li = doc.createElement('li');
        var button = element('button', 'proof-search-hit');
        button.type = 'button';
        var where = entry.lower.indexOf(terms[0]);
        var start = Math.max(0, where - 60), end = Math.min(entry.text.length, where + terms[0].length + 90);
        button.appendChild(element('strong', '', entry.key));
        button.appendChild(element('span', 'proof-search-where', 'in ' + entry.owner + ' \\u203a ' + entry.title));
        var snippet = element('span', 'proof-search-snippet');
        snippet.appendChild(doc.createTextNode((start > 0 ? '\\u2026' : '') + entry.text.slice(start, where)));
        var mark = doc.createElement('mark');
        mark.textContent = entry.text.slice(where, where + terms[0].length);
        snippet.appendChild(mark);
        snippet.appendChild(doc.createTextNode(entry.text.slice(where + terms[0].length, end) + (end < entry.text.length ? '\\u2026' : '')));
        button.appendChild(snippet);
        button.addEventListener('click', function () { showDetail(entry.detail, entry.section, entry.obligationId ? null : entry.key,
          entry.obligationId ? {obligationId:entry.obligationId} : null); });
        li.appendChild(button);
        results.appendChild(li);
      })(matches[i]);
    }
  }
  var timer = null;
  if (searchInput) {
    searchInput.addEventListener('input', function () { if (timer) clearTimeout(timer); timer = setTimeout(runSearch, 120); });
    searchInput.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') { runSearch(); var first = results.querySelector('button'); if (first) first.click(); }
    });
    runSearch();
  }

  // Read-only work links use the same exact reader locations as search.
  var work = projection.worklist, workList = doc.getElementById('proof-work-list');
  if (work && workList) {
    var unfinished = work.tasks.filter(function (task) { return task.required && task.state !== 'satisfied'; });
    var taskById = {}; work.tasks.forEach(function (task) { taskById[task.id] = task; });
    var workShown = 0, moreWork = doc.getElementById('proof-work-more');
    function showWork() {
      var end = Math.min(workShown + 20, unfinished.length);
      for (; workShown < end; workShown++) {
        (function (task) {
          var li = doc.createElement('li'), button = doc.createElement('button');
          button.type = 'button'; button.textContent = task.label + ' (' + humanWorkState(task.state) + ')';
          button.disabled = !task.location;
          button.setAttribute('data-work-task', task.id);
          if (task.location) button.addEventListener('click', function () {
            showDetail(task.location.detail_key, task.location.section_key, null, {obligationId:task.location.obligation_id});
          });
          li.appendChild(button);
          var explanation = element('p', 'proof-muted',
            {primary:'Primary checker', independent:'Independent checker', coordinator:'Coordinator'}[task.role]);
          if (task.waiting_on && task.waiting_on.length) {
            var waitingLabels = task.waiting_on.map(function (id) { return taskById[id] ? taskById[id].label : id; });
            explanation.textContent += '. Waiting for: ' + waitingLabels.slice(0, 3).join('; ') +
              (waitingLabels.length > 3 ? '; and ' + (waitingLabels.length - 3) + ' more' : '') + '.';
          }
          if (task.outcome) explanation.textContent += ' Local outcome: ' + task.outcome +
            (task.freshness ? ' (' + task.freshness.split('_').join(' ') + ')' : '') + '.';
          if (task.dependency_support) explanation.textContent += ' Premise support: ' + task.dependency_support + '.';
          li.appendChild(explanation);
          if (task.next_action) { var note = doc.createElement('p'); note.textContent = task.next_action; li.appendChild(note); }
          workList.appendChild(li);
        })(unfinished[workShown]);
      }
      doc.getElementById('proof-work-status').textContent = unfinished.length ?
        'Snapshot ' + work.revision + ': showing ' + workShown + ' of ' + unfinished.length + ' unfinished examinations.' :
        (work.analysis_complete ? 'Snapshot ' + work.revision + ': no unfinished examinations. Review the audit summary for remaining limits.' : 'Work analysis is incomplete.');
      moreWork.hidden = workShown >= unfinished.length;
    }
    function humanWorkState(state) { return {ready:'ready', waiting:'waiting for prerequisites', needs_coordinator:'coordinator action needed'}[state] || state; }
    moreWork.addEventListener('click', showWork); showWork();
    var actions = doc.getElementById('proof-work-actions');
    work.coordinator_actions.forEach(function (action) { var li = doc.createElement('li'); li.textContent = action.message; actions.appendChild(li); });
    if (work.diagnostics_truncated) { var moreActions = doc.createElement('li'); moreActions.textContent =
      'Showing the first ' + work.coordinator_actions.length + ' of ' + work.diagnostic_count + ' coordinator actions.'; actions.appendChild(moreActions); }
  }

  // Deep link: #proof-detail-N opens that detail.
  if (location.hash && location.hash.indexOf('#proof-detail-') === 0) {
    var linked = doc.getElementById(location.hash.slice(1));
    if (linked && linked.hasAttribute('data-proof-detail')) showDetail(linked.getAttribute('data-proof-detail'), null, null, { silent: true });
  }
})();`;

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = main(process.argv.slice(2));
}
