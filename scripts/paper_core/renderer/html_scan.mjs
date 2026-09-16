// Minimal hand-written HTML tokenizer used to verify the emitted artifact.
//
// It is deliberately small: it understands comments, doctype, start tags with
// quoted or unquoted attributes, end tags, self-closing tags (as emitted inside
// SVG), void elements, and the raw-text content of <script> and <style>. That is
// enough to check the DOM contract the Python caller enforces with html.parser.
// It is not a general HTML5 parser and must not be used to sanitize input.

const VOID_ELEMENTS = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr']);
const RAW_TEXT_ELEMENTS = new Set(['script', 'style']);
const NAMED_ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' };

export function decodeEntities(text) {
  return String(text).replace(/&(#[xX][0-9a-fA-F]+|#[0-9]+|[A-Za-z][A-Za-z0-9]*);/g, (match, body) => {
    if (body[0] === '#') {
      const code = body[1] === 'x' || body[1] === 'X' ? parseInt(body.slice(2), 16) : parseInt(body.slice(1), 10);
      return Number.isFinite(code) && code >= 0 && code <= 0x10ffff && !(code >= 0xd800 && code <= 0xdfff) ? String.fromCodePoint(code) : match;
    }
    return Object.hasOwn(NAMED_ENTITIES, body) ? NAMED_ENTITIES[body] : match;
  });
}

const TAG_OPEN = /<([A-Za-z][A-Za-z0-9:_.-]*)/y;
const ATTRIBUTE = /\s*([^\s"'<>\/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/y;

// Returns { html, elements, root }. Every element records its tag (lowercase),
// decoded attributes (names lowercased, first occurrence wins), source offsets,
// parent, children, and for raw-text elements the verbatim text.
export function scanHtml(html) {
  const elements = [];
  const root = { tag: '#root', attrs: {}, children: [], parent: null, start: 0, openEnd: 0, closeStart: html.length, closeEnd: html.length };
  const stack = [];
  let i = 0;
  const length = html.length;
  while (i < length) {
    const lt = html.indexOf('<', i);
    if (lt < 0) break;
    if (html.startsWith('<!--', lt)) {
      const end = html.indexOf('-->', lt + 4);
      i = end < 0 ? length : end + 3;
      continue;
    }
    if (html.startsWith('<!', lt) || html.startsWith('<?', lt)) {
      const end = html.indexOf('>', lt);
      i = end < 0 ? length : end + 1;
      continue;
    }
    if (html.startsWith('</', lt)) {
      const end = html.indexOf('>', lt);
      if (end < 0) break;
      const name = html.slice(lt + 2, end).trim().toLowerCase();
      for (let s = stack.length - 1; s >= 0; s -= 1) {
        if (stack[s].tag !== name) continue;
        for (let k = stack.length - 1; k >= s; k -= 1) {
          stack[k].closeStart = lt;
          stack[k].closeEnd = end + 1;
        }
        stack.length = s;
        break;
      }
      i = end + 1;
      continue;
    }
    TAG_OPEN.lastIndex = lt;
    const open = TAG_OPEN.exec(html);
    if (!open) { i = lt + 1; continue; }
    const tag = open[1].toLowerCase();
    let position = TAG_OPEN.lastIndex;
    const attrs = {};
    let selfClosing = false;
    for (;;) {
      while (position < length && /\s/.test(html[position])) position += 1;
      if (position >= length) break;
      if (html[position] === '>') { position += 1; break; }
      if (html.startsWith('/>', position)) { selfClosing = true; position += 2; break; }
      ATTRIBUTE.lastIndex = position;
      const attribute = ATTRIBUTE.exec(html);
      if (!attribute || ATTRIBUTE.lastIndex === position) { position += 1; continue; }
      const name = attribute[1].toLowerCase();
      const value = attribute[2] ?? attribute[3] ?? attribute[4] ?? '';
      if (!Object.hasOwn(attrs, name)) attrs[name] = decodeEntities(value);
      position = ATTRIBUTE.lastIndex;
    }
    const parent = stack.length ? stack[stack.length - 1] : root;
    const element = { tag, attrs, start: lt, openEnd: position, closeStart: length, closeEnd: length, parent, children: [], selfClosing, text: null, index: elements.length };
    parent.children.push(element);
    elements.push(element);
    if (RAW_TEXT_ELEMENTS.has(tag) && !selfClosing) {
      const closer = new RegExp(`</${tag}(?=[\\s/>])`, 'ig');
      closer.lastIndex = position;
      const match = closer.exec(html);
      const closeStart = match ? match.index : length;
      const gt = match ? html.indexOf('>', match.index) : -1;
      element.text = html.slice(position, closeStart);
      element.closeStart = closeStart;
      element.closeEnd = gt < 0 ? length : gt + 1;
      i = element.closeEnd;
      continue;
    }
    if (selfClosing || VOID_ELEMENTS.has(tag)) {
      element.closeStart = position;
      element.closeEnd = position;
      i = position;
      continue;
    }
    stack.push(element);
    i = position;
  }
  return { html, elements, root };
}

// Inner text with tags removed and entities decoded (raw text for script/style).
export function textOf(scan, element) {
  if (element.text !== null && element.text !== undefined) return element.text;
  const inner = scan.html.slice(element.openEnd, element.closeStart);
  return decodeEntities(inner.replace(/<!--[\s\S]*?-->/g, '').replace(/<[^>]*>/g, ''));
}

export function innerHtml(scan, element) {
  return scan.html.slice(element.openEnd, element.closeStart);
}

export function* descendants(element) {
  for (const child of element.children) {
    yield child;
    yield* descendants(child);
  }
}

export function hasAttr(element, name) {
  return Object.hasOwn(element.attrs, name);
}

export function findAll(scan, predicate) {
  return scan.elements.filter(predicate);
}

export function withAttr(scan, name, value) {
  return scan.elements.filter((element) => hasAttr(element, name) && (value === undefined || element.attrs[name] === value));
}

export function sameMultiset(actual, expected) {
  const a = [...actual].map(String).sort();
  const b = [...expected].map(String).sort();
  return a.length === b.length && a.every((value, index) => value === b[index]);
}
