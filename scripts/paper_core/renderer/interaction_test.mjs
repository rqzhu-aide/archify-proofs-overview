// Deterministic adapter contract simulation. This does not claim browser/CSS acceptance.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { renderProjection } from './render_projection.mjs';
import { scanHtml, textOf } from './html_scan.mjs';

function runFixture(name, withWork = false) {
  const input = JSON.parse(readFileSync(new URL(`./fixtures/${name}`, import.meta.url), 'utf8'));
  if (withWork) {
    input.projection.projection_version = 2;
    const target = input.projection.records.find(row => row.body.kind === 'intermediate_result') ||
      input.projection.records.find(row => row.ref.collection === 'uses');
    const ownerId = target.body.owner_id || input.projection.nodes[input.projection.nodes.length - 1].id;
    const detailKey = 'item:' + ownerId;
    const detail = input.projection.details[detailKey];
    const section = detail.sections.find(row => row.record_refs.some(ref => ref.id === target.ref.id)) || detail.sections[0];
    const baseObligation = input.projection.obligations[0];
    const tasks = [];
    for (let index = 0; index < 25; index++) {
      const id = 'obl_work_' + index;
      input.projection.obligations.push({...baseObligation, id, target:{collection:target.ref.collection, id:target.ref.id}});
      section.obligation_ids.push(id);
      tasks.push({id, target:{collection:target.ref.collection, id:target.ref.id}, role:'primary', required:true,
        label:'Hidden bound: local derivation ' + index, state:index ? 'waiting' : 'ready',
        waiting_on:index ? ['obl_work_0'] : [], next_action:index ? null : 'Read the last inequality.',
        outcome:null, freshness:null, dependency_support:'conditional',
        location:{ref:target.ref, detail_key:detailKey, section_key:section.key, obligation_id:id}});
    }
    input.projection.worklist = {revision:input.projection.snapshot_revision, analysis_complete:true, tasks,
      coordinator_actions:[{message:'An upstream proof is incomplete; local support remains conditional.'}]};
  }
  input.projection.summary.limitations = ['Coverage incomplete: a < b & c.'];
  const { html } = renderProjection(Buffer.from(JSON.stringify(input)));
  const scan = scanHtml(html);
  const scripts = scan.elements.filter(e => e.tag === 'script' && e.attrs.type !== 'application/json');
  scripts.forEach((script, i) => new vm.Script(script.text, { filename: `generated-${i}.js` }));
  const native = scripts.find(s => s.text.includes('var Archify = {}')).text;
  for (const api of ['Archify.focus =', 'Archify.view =', 'Archify.exportMenu =', 'Archify.presentation ='])
    assert(native.includes(api), `Native runtime missing ${api}`);
  assert(native.includes("probe.className = 'proof-svg'"), 'Export theme probes must resolve audit variables.');
  const filter = native.match(/if \((\/\(\^\|,\).*?\/)\.test\(sel\)\)/);
  assert(filter, 'Native export style filter must remain available.');
  const selectorFilter = vm.runInNewContext(filter[1]);
  for (const selector of ['.proof-node-box', '.proof-node.s-red', '.proof-edge.s-amber', '.k-theorem', '.proof-arrowhead'])
    assert(selectorFilter.test(selector), `Export would omit verdict/kind style ${selector}`);

  const callbacks = new Map(), observers = [], moves = [];
  let timer = 0, document;
  class Element {
    constructor(row = {}) {
      this.tagName = row.tag || 'div'; this.attrs = { ...row.attrs }; this.children = [];
      this.parentElement = null; this.listeners = []; this._text = row.text || '';
      this.hidden = Object.hasOwn(this.attrs, 'hidden'); this.checked = false;
      this.clientWidth = 600; this.clientHeight = 300;
      const vb = (this.attrs.viewbox || '0 0 600 300').split(' ').map(Number);
      this.viewBox = { baseVal: { x: vb[0], y: vb[1], width: vb[2], height: vb[3] } };
      for (const child of row.children || []) this.appendChild(new Element(child));
    }
    get id() { return this.attrs.id; }
    get parentNode() { return this.parentElement; }
    get className() { return this.attrs.class || ''; }
    set className(value) { this.attrs.class = value; }
    get classList() {
      const self = this;
      return { contains(c) { return self.className.split(/\s+/).includes(c); },
        add(c) { if (!this.contains(c)) self.className = `${self.className} ${c}`.trim(); },
        remove(c) { self.className = self.className.split(/\s+/).filter(s => s !== c).join(' '); },
        toggle(c, on) { if (on === undefined) on = !this.contains(c); if (on) this.add(c); else this.remove(c); } };
    }
    get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
    set textContent(value) { this._text = value; this.children.forEach(c => c.parentElement = null); this.children = []; }
    getAttribute(key) { return this.attrs[key] ?? null; }
    hasAttribute(key) { return Object.hasOwn(this.attrs, key); }
    setAttribute(key, value) { this.attrs[key] = String(value); }
    removeAttribute(key) { delete this.attrs[key]; }
    appendChild(child) {
      if (child.parentElement) child.parentElement.children.splice(child.parentElement.children.indexOf(child), 1);
      this.children.push(child); child.parentElement = this; return child;
    }
    cloneNode(deep) {
      const clone = new Element({tag:this.tagName, attrs:this.attrs, text:this._text});
      if (deep) this.children.forEach(child => clone.appendChild(child.cloneNode(true)));
      return clone;
    }
    get content() {
      const fragment = new Element({tag:'#fragment'});
      this.children.forEach(child => fragment.appendChild(child.cloneNode(true)));
      return fragment;
    }
    replaceChildren(child) {
      this.textContent = '';
      if (child.tagName === '#fragment') [...child.children].forEach(entry => this.appendChild(entry));
      else this.appendChild(child);
    }
    insertAdjacentElement(position, child) {
      assert.equal(position, 'afterend');
      if (child.parentElement) child.parentElement.children.splice(child.parentElement.children.indexOf(child), 1);
      const p = this.parentElement; p.children.splice(p.children.indexOf(this) + 1, 0, child); child.parentElement = p;
    }
    contains(e) { return e === this || this.children.some(c => c.contains(e)); }
    matches(selector) {
      const tag = selector.match(/^[\w-]+/);
      if (tag && this.tagName !== tag[0]) return false;
      for (const match of selector.matchAll(/#([\w-]+)|\.([\w-]+)|\[([^\]=]+)(?:="([^"]*)")?\]/g)) {
        if (match[1] && this.id !== match[1]) return false;
        if (match[2] && !this.classList.contains(match[2])) return false;
        if (match[3] && (!this.hasAttribute(match[3]) || match[4] !== undefined && this.getAttribute(match[3]) !== match[4])) return false;
      }
      return true;
    }
    closest(selector) {
      for (let e = this; e; e = e.parentElement) if (selector.split(',').some(s => e.matches(s.trim()))) return e;
      return null;
    }
    querySelectorAll(selector) {
      const result = [], selectors = selector.split(',').map(s => s.trim());
      const walk = e => e.children.forEach(child => { if (selectors.some(s => child.matches(s))) result.push(child); walk(child); });
      walk(this); return result;
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    addEventListener(type, callback, options) { this.listeners.push({ type, callback, capture: options === true }); }
    scrollIntoView() { moves.push(this.id || this.tagName); }
    focus() { document.activeElement = this; }
    click() { dispatch(this, { type: 'click' }); }
  }
  const row = el => ({ tag: el.tag, attrs: el.attrs, text: el.tag === 'script' || el.tag === 'style' ? el.text : '', children: el.children.map(row) });
  document = new Element(row(scan.root));
  document.documentElement = document.querySelector('html'); document.body = document.querySelector('body');
  document.getElementById = id => document.querySelector('#' + id);
  document.createElement = tag => new Element({ tag });
  document.createTextNode = text => new Element({ tag: '#text', text });
  const svg = document.querySelector('svg.proof-svg'), container = document.querySelector('.diagram-container');
  const chip = document.getElementById('focus-chip');
  const schedule = cb => { callbacks.set(++timer, cb); return timer; };
  const flush = () => {
    for (let i = 0; callbacks.size && i < 20; i++) { const list = [...callbacks.values()]; callbacks.clear(); list.forEach(cb => cb()); }
    assert.equal(callbacks.size, 0, 'Adapter never settled.');
  };
  const notify = attribute => observers.forEach(o => { if (o.options.attributeFilter.includes(attribute)) schedule(o.callback); });
  function dispatch(target, event) {
    event.target = target; event.preventDefault = () => {}; event.stopPropagation = () => { event.stopped = true; };
    const path = []; for (let e = target; e; e = e.parentElement) path.push(e);
    for (const capture of [true, false]) for (const e of capture ? [...path].reverse() : path) {
      e.listeners.filter(l => l.type === event.type && l.capture === capture).forEach(l => l.callback(event));
      if (event.stopped) return;
    }
  }
  let active = null, relation = null, reset = 0, centers = 0;
  const focus = {
    active: () => active, relationship: () => relation,
    set(id) { active = id; relation = null; chip.hidden = false; svg.setAttribute('data-focus-active', id); notify('data-focus-active'); },
    clear() { active = null; relation = null; chip.hidden = true; svg.removeAttribute('data-focus-active'); notify('data-focus-active'); },
    inspectRelationshipById(id) {
      relation = input.projection.connections.find(c => c.id === id); active = relation.from; chip.hidden = false;
      svg.setAttribute('data-relationship-pin-active', id); notify('data-relationship-pin-active'); return true;
    },
  };
  const Archify = { focus, view: { centerAt(x, y) { assert(Number.isFinite(x) && Number.isFinite(y)); centers++; }, reset() { reset++; } } };
  const context = vm.createContext({ document, window: { Archify, addEventListener() {} }, location: { hash: '' },
    MutationObserver: class { constructor(callback) { this.callback = callback; } observe(target, options) { observers.push({ callback: this.callback, options }); } },
    requestAnimationFrame: schedule, setTimeout: schedule, clearTimeout: id => callbacks.delete(id) });
  const runtime = scripts.find(s => s.attrs.id === 'proof-runtime');
  vm.runInContext(runtime.text, context); flush();
  const detail = key => document.querySelector(`[data-proof-detail="${key}"]`);
  if (withWork) {
    const panel = document.getElementById('proof-work-list'), more = document.getElementById('proof-work-more');
    assert.equal(panel.children.length, 20, 'The worklist initially creates only twenty rows.');
    assert.equal(more.hidden, false);
    assert(panel.children[0].textContent.includes('Primary checker'), 'Work rows explain who performs the examination.');
    assert(panel.children[1].textContent.includes('Waiting for:'), 'Waiting rows name recorded prerequisites.');
    assert(panel.children[0].textContent.includes('Premise support: conditional'), 'Readiness must not replace proof support.');
    panel.querySelector('[data-work-task="obl_work_0"]').click(); flush();
    assert.equal(document.querySelector('.is-target').getAttribute('data-obligation-id'), 'obl_work_0',
      'A work row must focus the exact obligation rather than its nearby record.');
    if (svg) assert.equal(active, input.projection.worklist.tasks[0].location.detail_key.slice('item:'.length),
      'Worklist navigation selects the existing major owner.');
    more.click(); flush(); assert.equal(panel.children.length, 25); assert.equal(more.hidden, true);
    const search = document.getElementById('proof-search'); search.value = 'obl_work_24';
    dispatch(search, {type:'input'}); flush();
    const hit = document.getElementById('proof-search-results').querySelector('button');
    assert(hit, 'A copied obligation ID must be searchable.'); hit.click(); flush();
    assert.equal(document.querySelector('.is-target').getAttribute('data-obligation-id'), 'obl_work_24');
    assert(document.getElementById('proof-work-status').textContent.includes('Snapshot '),
      'The pending list names its immutable snapshot.');
  }
  const first = input.projection.nodes[0];
  if (svg) {
    assert(!container.contains(chip));
    assert(!container.contains(document.getElementById('proof-details')));
    focus.set(first.id); flush();
    assert(detail(first.detail_key).classList.contains('is-active'));
    const edge = input.projection.connections[0];
    focus.inspectRelationshipById(edge.id); flush();
    assert(detail(edge.detail_key).classList.contains('is-active'), 'Native edge selection must open its canonical evidence.');
    assert(!detail(first.detail_key).classList.contains('is-active'));
    focus.clear(); flush();
    assert(!detail(edge.detail_key).classList.contains('is-active'), 'Native close must clear the reader.');
    const main = document.querySelector('[data-proof-main]'); main.click(); flush();
    assert.equal(active, main.getAttribute('data-proof-main'));
    document.getElementById('proof-full-structure').click(); flush(); assert.equal(reset, 1);
    document.getElementById('proof-readable-view').click(); assert(centers >= 2);
    const previousCenters = centers;
    const showInGraph = document.querySelector('[data-focus-node]'); showInGraph.click(); flush();
    assert.equal(active, showInGraph.getAttribute('data-focus-node'));
    assert(centers > previousCenters, 'Show in graph must center the native camera on the requested item.');
    const beforeKeyboard = centers, selectedBeforeKeyboard = active;
    dispatch(svg.querySelector('[data-node-id]'), { type: 'keydown', key: 'End' }); flush();
    assert(centers > beforeKeyboard, 'Keyboard navigation must bring off-screen items into the native camera.');
    assert.equal(active, selectedBeforeKeyboard, 'Moving keyboard focus must not open a different evidence panel before Enter.');
    const badge = svg.querySelector('[data-edge-badge]');
    if (badge) { badge.click(); flush(); assert.equal(relation.id, badge.getAttribute('data-edge-badge')); }
  } else {
    assert(document.getElementById('btn-export').disabled, 'Index fallback must not export an empty hidden graph.');
    document.querySelector('[data-proof-main]').click();
  }
  const hidden = input.projection.records.find(r => r.body.kind === 'intermediate_result');
  const record = hidden || input.projection.records.find(r => r.ref.collection === 'uses');
  const search = document.getElementById('proof-search'); search.value = record.ref.id;
  dispatch(search, { type: 'input' }); flush();
  const hit = document.getElementById('proof-search-results').querySelector('button');
  assert(hit, 'Hidden/canonical record must be searchable.'); hit.click(); flush();
  assert(document.querySelector('.is-target'), 'Search must reach the actual canonical record section.');
  const notices = scan.elements.filter(e => Object.hasOwn(e.attrs, 'data-proof-limitation'));
  assert.deepEqual(notices.map(e => textOf(scan, e)), input.projection.summary.limitations);
  return { fixture: name, compiled_scripts: scripts.length, canonical_reader_bridge: true, worklist:withWork };
}

console.log(JSON.stringify([
  ...['dag_small.json', 'index_fallback.json', 'long_math.json'].map(name => runFixture(name)),
  runFixture('dag_small.json', true), runFixture('index_fallback.json', true),
]));
