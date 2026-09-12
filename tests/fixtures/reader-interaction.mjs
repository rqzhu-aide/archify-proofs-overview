// Minimal adapter contract simulation, not a browser or CSS layout engine.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const packet = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
packet.scripts.forEach((source, index) => new vm.Script(source, { filename: `generated-script-${index}.js` }));
if (process.argv[3] === 'compile') {
  console.log(JSON.stringify({ compiled_scripts: packet.scripts.length }));
  process.exit(0);
}

const observers = [], callbacks = new Map(), scrolls = [];
let callbackId = 0;
class Element {
  constructor(row = {}) {
    this.tagName = row.tag || 'div'; this.attrs = { ...(row.attrs || {}) };
    this.children = []; this.parentElement = null; this.listeners = new Map();
    this.innerHTML = row.innerHTML || ''; this.textContent = row.text || '';
    this.hidden = Object.hasOwn(this.attrs, 'hidden'); this.open = false;
    this.style = { removeProperty(key) { delete this[key]; }, setProperty(key, value) { this[key] = value; } };
    this.clientWidth = 600; this.clientHeight = 110; this.offsetWidth = 460; this.offsetHeight = 240;
    this.viewBox = { baseVal: { x: 0, y: 0, width: 600, height: 110 } };
    (row.children || []).forEach(child => this.appendChild(new Element(child)));
  }
  get id() { return this.attrs.id; }
  set id(value) { this.attrs.id = value; }
  get parentNode() { return this.parentElement; }
  getAttribute(key) { return this.attrs[key] ?? null; }
  hasAttribute(key) { return Object.hasOwn(this.attrs, key); }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  removeAttribute(key) { delete this.attrs[key]; }
  appendChild(child) {
    if (child.parentElement) child.parentElement.children.splice(child.parentElement.children.indexOf(child), 1);
    this.children.push(child); child.parentElement = this; return child;
  }
  insertAdjacentElement(position, element) {
    assert.equal(position, 'afterend');
    const parent = this.parentElement;
    if (element.parentElement) element.parentElement.children.splice(element.parentElement.children.indexOf(element), 1);
    parent.children.splice(parent.children.indexOf(this) + 1, 0, element); element.parentElement = parent;
    return element;
  }
  contains(element) { return element === this || this.children.some(child => child.contains(element)); }
  matches(selector) {
    if (selector.startsWith('#')) return this.id === selector.slice(1);
    if (selector.startsWith('.')) return (this.attrs.class || '').split(/\s+/).includes(selector.slice(1));
    const match = selector.match(/^([\w-]+)?(?:\[([^=\]]+)(?:="([^"]*)")?\])?$/);
    assert(match, `Unsupported stub selector: ${selector}`);
    return (!match[1] || this.tagName === match[1]) && (!match[2] || this.hasAttribute(match[2]) && (match[3] === undefined || this.getAttribute(match[2]) === match[3]));
  }
  closest(selector) {
    for (let element = this; element; element = element.parentElement)
      if (selector.split(',').some(part => element.matches(part.trim()))) return element;
    return null;
  }
  querySelectorAll(selector) {
    const results = [];
    const matches = element => selector.split(',').some(part => {
      const terms = part.trim().split(/\s+/), last = terms.pop();
      if (!element.matches(last)) return false;
      let ancestor = element.parentElement;
      while (terms.length) {
        const term = terms.pop();
        while (ancestor && !ancestor.matches(term)) ancestor = ancestor.parentElement;
        if (!ancestor) return false;
        ancestor = ancestor.parentElement;
      }
      return true;
    });
    const walk = element => element.children.forEach(child => {
      if (matches(child)) results.push(child);
      if (child.tagName !== 'template') walk(child);
    });
    walk(this); return results;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  addEventListener(type, callback, options) {
    const list = this.listeners.get(type) || [];
    list.push({ callback, capture: options === true || options?.capture === true }); this.listeners.set(type, list);
  }
  scrollIntoView() { scrolls.push(this.id || this.tagName); }
  getBoundingClientRect() { return { left: 0, top: 0, bottom: this.clientHeight, right: this.clientWidth, width: this.clientWidth, height: this.clientHeight }; }
  dispatchEvent(event) { dispatch(this, event); return true; }
}

const document = new Element(packet.document), window = new Element({ tag: 'window' });
document.getElementById = id => document.querySelector('#' + id);
document.createElement = tag => new Element({ tag });
document.body = document.querySelector('body'); document.documentElement = document.querySelector('html');
window.innerWidth = 1200; window.innerHeight = 800;
const svg = document.querySelector('.diagram-container svg'), container = svg.parentElement;
const chip = document.getElementById('focus-chip');
assert(container.contains(chip), 'Fixture must begin with the upstream embedded details panel.');

function schedule(callback) { callbacks.set(++callbackId, callback); return callbackId; }
function flush() {
  for (let turn = 0; callbacks.size && turn < 20; turn++) {
    const pending = [...callbacks.values()]; callbacks.clear(); pending.forEach(callback => callback());
  }
  assert.equal(callbacks.size, 0, 'Adapter did not settle.');
}
function notifyFocus() { observers.forEach(callback => schedule(callback)); }
function dispatch(target, event) {
  event.target = target; event.preventDefault ||= () => {};
  event.stopPropagation = () => { event.stopped = true; };
  const path = []; for (let element = target; element; element = element.parentElement) path.push(element);
  for (const capture of [true, false]) {
    for (const element of capture ? [...path].reverse() : path) {
      for (const listener of element.listeners.get(event.type) || [])
        if (listener.capture === capture) listener.callback(event);
      if (event.stopped) return;
    }
  }
}
let active = null, relation = null;
const focus = {
  active: () => active, relationship: () => relation,
  set(id) { active = id; relation = null; chip.hidden = false; svg.setAttribute('data-focus-active', id); notifyFocus(); },
  clear() { active = null; relation = null; chip.hidden = true; svg.removeAttribute('data-focus-active'); notifyFocus(); },
  inspectRelationshipById(id) { active = 'result'; relation = { id }; chip.hidden = false; notifyFocus(); },
  reposition() {},
};
// This is the upstream focus contract, not a replacement implementation under test.
svg.addEventListener('click', event => {
  const node = event.target.closest('[data-node-id]');
  if (node) focus.set(node.getAttribute('data-node-id'));
});
document.getElementById('btn-focus-clear').addEventListener('click', () => focus.clear());
const context = vm.createContext({
  document, window, Archify: { focus, view: { centerAt() {}, reset() {} } },
  MutationObserver: class { constructor(callback) { this.callback = callback; } observe() { observers.push(this.callback); } },
  requestAnimationFrame: schedule, cancelAnimationFrame: id => callbacks.delete(id),
  setTimeout: schedule, clearTimeout: id => callbacks.delete(id),
  MouseEvent: class { constructor(type, options) { this.type = type; Object.assign(this, options); } },
});
const runtime = packet.scripts.find(source => source.includes("JSON.parse(document.getElementById('proof-overview-data').textContent)"));
assert(runtime, 'Generated proof runtime is missing.');
vm.runInContext(runtime, context); flush();
assert(!container.contains(chip), 'Details remain constrained by the shallow diagram instead of following it in document flow.');
assert.equal(chip.parentElement, container.parentElement, 'Details must remain in the reader document beside the diagram.');
const panel = document.getElementById('proof-selection');
assert(chip.contains(panel), 'The statement reader is missing from the details panel.');

dispatch(svg.querySelector('[data-node-id="assumption"]'), { type: 'click' }); flush();
assert(!chip.hidden); assert.match(panel.innerHTML, /ASSUMPTION_TEXT/);
assert(!panel.innerHTML.includes('RESULT_TEXT'));
assert.equal(scrolls.at(-1), 'focus-chip', 'Selecting an ordinary item must reveal its details.');

dispatch(document.querySelector('[data-proof-main="result"]'), { type: 'click' }); flush();
assert.match(panel.innerHTML, /RESULT_TEXT/);
assert.equal(scrolls.at(-1), 'focus-chip', 'Result navigation must not scroll the graph over the details.');

dispatch(svg.querySelector('[data-proof-use="independence-use"]'), { type: 'click' }); flush();
assert.match(panel.innerHTML, /RELATION_TEXT/);
assert.equal(scrolls.at(-1), 'focus-chip');
dispatch(document.getElementById('btn-focus-clear'), { type: 'click' }); flush();
assert(chip.hidden); assert.equal(panel.innerHTML, '');
console.log(JSON.stringify({ details_outside_diagram: true, checked_selections: ['assumption', 'result', 'independence-use'], close_cleared_details: true }));
