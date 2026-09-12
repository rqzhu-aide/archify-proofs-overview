// Deterministic geometry and observer feedback, not a browser layout engine.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const packet = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const mode = process.argv[3];
assert(['emitted', 'upstream'].includes(mode));
const ratio = packet.viewBox.width / packet.viewBox.height;
assert(ratio >= 1.55, 'The emitted fixture must exercise the wide reader.');
const callbacks = new Map(), observers = [], widthWrites = [];
let nextFrame = 0, extraHeaderHeight = 0, statementHeight = 0;
const schedule = callback => { callbacks.set(++nextFrame, callback); return nextFrame; };

class Element {
  constructor(height = 0) {
    this.height = height; this.hidden = false; this.attrs = new Map(); this.children = new Map();
    const properties = new Map();
    this.style = {
      getPropertyValue: name => properties.get(name) || '',
      setProperty: (name, value) => {
        if (name === '--archify-reader-width' && properties.get(name) !== value) widthWrites.push(Number.parseFloat(value));
        properties.set(name, value);
      },
      removeProperty: name => properties.delete(name),
    };
  }
  getAttribute(name) { return this.attrs.get(name) ?? null; }
  setAttribute(name, value) { this.attrs.set(name, String(value)); }
  removeAttribute(name) { this.attrs.delete(name); }
  querySelector(selector) { return this.children.get(selector) || null; }
  getBoundingClientRect() { return { width: readerWidth(), height: this.height }; }
}

const html = new Element(), body = new Element(), shell = new Element();
const diagram = new Element(), svg = new Element();
const header = new Element(60), guided = new Element(30), cards = new Element(30);
svg.viewBox = { baseVal: packet.viewBox };
diagram.children.set(':scope > svg', svg);
shell.children = new Map([['.header', header], ['.guided-views', guided], ['.cards', cards]]);
const windowListeners = new Map();
const window = {
  innerWidth: 1280, innerHeight: 800,
  matchMedia: () => ({ matches: false }),
  addEventListener(name, callback) {
    const list = windowListeners.get(name) || []; list.push(callback); windowListeners.set(name, list);
  },
  getComputedStyle(element) {
    return { display: 'block', marginTop: '0', marginBottom: '0',
      paddingLeft: element === body ? '24px' : '0', paddingRight: element === body ? '24px' : '0',
      paddingTop: element === body ? '12px' : '0', paddingBottom: element === body ? '12px' : '0',
      borderLeftWidth: '0', borderRightWidth: '0', borderTopWidth: '0', borderBottomWidth: '0' };
  },
};
function readerWidth() {
  return Number.parseFloat(html.style.getPropertyValue('--archify-reader-width')) || Math.min(1920, window.innerWidth - 48);
}
function documentHeight() {
  // The detached statement panel contributes to page height, while the old
  // measure() counts only header/guided/cards before trying to remove overflow.
  return 24 + header.height + extraHeaderHeight + guided.height + cards.height + readerWidth() / ratio + statementHeight;
}
header.getBoundingClientRect = () => ({ width: readerWidth(), height: header.height + extraHeaderHeight });
diagram.getBoundingClientRect = () => ({ width: readerWidth(), height: readerWidth() / ratio });
shell.getBoundingClientRect = () => ({ width: readerWidth(), height: documentHeight() - 24 });
for (const element of [html, body]) {
  Object.defineProperty(element, 'scrollHeight', { get: () => Math.max(window.innerHeight, documentHeight()) });
  Object.defineProperty(element, 'scrollWidth', { get: () => window.innerWidth });
}
function geometry(element) { const rect = element.getBoundingClientRect(); return `${rect.width}|${rect.height}`; }
function deliverResizeObservers() {
  for (const observer of observers) {
    const changed = [];
    for (const [element, previous] of observer.elements) {
      const current = geometry(element);
      if (current !== previous) { observer.elements.set(element, current); changed.push({ target: element }); }
    }
    if (changed.length) observer.callback(changed);
  }
}
const document = { documentElement: html, body,
  querySelector: selector => new Map([['.container', shell], ['.diagram-container', diagram]]).get(selector) || null };
const Archify = {};
const context = vm.createContext({ Archify, document, window,
  requestAnimationFrame: schedule, cancelAnimationFrame: id => callbacks.delete(id),
  ResizeObserver: class {
    constructor(callback) { this.callback = callback; this.elements = new Map(); observers.push(this); }
    observe(element) { this.elements.set(element, geometry(element)); }
  },
});
vm.runInContext(packet[mode], context, { filename: `${mode}-reader-layout.js`, timeout: 1000 });
const layout = Archify.readerLayout;
for (const method of ['measure', 'schedule', 'whenStable', 'active', 'receipt']) assert.equal(typeof layout[method], 'function');

async function settle(maximumFrames = 24) {
  let stable = false, error = null;
  layout.whenStable().then(result => { stable = result.stable; }, reason => { error = reason; });
  for (let frame = 0; frame < maximumFrames; frame++) {
    await Promise.resolve();
    const pending = [...callbacks.values()]; callbacks.clear();
    pending.forEach(callback => callback());
    // Real observed header/card boxes change width with their enclosing shell.
    deliverResizeObservers();
    await Promise.resolve();
    if (error) throw error;
    if (stable && callbacks.size === 0) return true;
  }
  return false;
}
function disclose(open) {
  extraHeaderHeight = open ? 32 : 0;
  statementHeight = open ? 1100 : 0;
  deliverResizeObservers();
}
const initialStable = await settle();
assert(initialStable, 'The collapsed reader did not settle before the disclosure.');
const initialWidth = layout.receipt().width, beforeDisclosure = widthWrites.length;
disclose(true);
const expandedStable = await settle();
const expandedWrites = widthWrites.slice(beforeDisclosure);

if (mode === 'upstream') {
  console.log(JSON.stringify({ initial_stable: initialStable, expanded_stable: expandedStable,
    width_changes: expandedWrites.length, expanded_widths: [...new Set(expandedWrites)].sort((a, b) => a - b) }));
} else {
  assert(expandedStable, `Opening details failed to settle: widths ${expandedWrites.join(', ')}`);
  const expandedWidth = layout.receipt().width, expandedOverflow = html.scrollHeight - window.innerHeight;
  disclose(false);
  assert(await settle(), 'Closing details failed to settle.');
  const collapsedWidth = layout.receipt().width, disclosureWrites = widthWrites.length - beforeDisclosure;
  window.innerWidth = 1120;
  windowListeners.get('resize').forEach(callback => callback());
  assert(await settle(), 'Desktop resize failed to settle.');
  const resizedWidth = layout.receipt().width;
  window.innerWidth = 820;
  windowListeners.get('resize').forEach(callback => callback());
  assert(await settle(), 'Mobile resize failed to settle.');
  console.log(JSON.stringify({ stable_states: ['collapsed', 'expanded', 'collapsed_again', 'resized', 'mobile'],
    desktop_widths: [initialWidth, expandedWidth, collapsedWidth, resizedWidth], expanded_overflow: expandedOverflow,
    disclosure_width_changes: disclosureWrites,
    mobile_override_cleared: !layout.active() && layout.receipt().width === 0 && html.style.getPropertyValue('--archify-reader-width') === '',
    wide_diagram_tagged: diagram.getAttribute('data-wide-diagram') === 'true' && html.getAttribute('data-diagram-shape') === 'wide' }));
}
