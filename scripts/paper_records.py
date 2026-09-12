"""Versioned paper records shared by JSON, SQLite, and the overview renderer.

Source bindings and comparison observations are evidence about recorded content,
not mathematical proof verdicts. Source payloads make historical exports portable.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from overview_math import render_text

KINDS = {"assumption", "definition", "lemma", "proposition", "theorem", "corollary", "external_result"}
USE_TYPES = {"dependency", "definition", "proof_argument"}


class RecordError(ValueError):
    """A record problem that can be corrected without changing the manuscript."""


def _fields(value, required, optional, context):
    if not isinstance(value, dict):
        raise RecordError(f"{context}: expected an object.")
    missing = set(required) - value.keys()
    extra = value.keys() - set(required) - set(optional)
    if missing or extra:
        detail = []
        if missing:
            detail.append("missing " + ", ".join(sorted(missing)))
        if extra:
            detail.append("unknown fields " + ", ".join(sorted(extra)))
        raise RecordError(f"{context}: {'; '.join(detail)}. Check the record contract.")


def _text(value, context, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise RecordError(f"{context}: expected {'text' if empty else 'nonempty text'}.")
    if any(ord(c) < 32 and c not in '\n\r\t' for c in value):
        raise RecordError(f"{context}: unexpected control character; check JSON escaping.")
    return value


def _id(value, context):
    _text(value, context)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", value):
        raise RecordError(f"{context}: use a letter followed by letters, digits, hyphens or underscores.")
    return value


def _rows(value, context):
    if not isinstance(value, list):
        raise RecordError(f"{context}: expected a list.")
    return value


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def snapshot_digest(data):
    """Identity of authored content, independent of appended comparisons/builds."""
    content = {k: v for k, v in data.items() if k not in {'snapshot_id', 'observations'}}
    if 'source_revision' in content:
        content['source_revision'] = {k: v for k, v in content['source_revision'].items() if k != 'created_at'}
    return _digest(content)


def _source_digest(files):
    return _digest(sorted(({k: row[k] for k in ('id', 'path', 'media_type', 'sha256')}
                           for row in files), key=lambda row: row['id']))


def _source_bytes(row):
    try:
        raw = base64.b64decode(row['content_base64'], validate=True)
    except (ValueError, TypeError) as exc:
        raise RecordError(f"Source {row.get('path', '?')}: invalid captured content.") from exc
    if _sha(raw) != row['sha256']:
        raise RecordError(f"Source {row['path']}: content hash does not match the captured bytes.")
    return raw


def _source_text(row):
    try:
        return _source_bytes(row).decode('utf-8-sig')
    except UnicodeError as exc:
        raise RecordError(f"Source {row['path']}: line anchors require UTF-8 text.") from exc


def _uncomment(text):
    return re.sub(r'(?<!\\)%[^\n]*', '', text)


def _capture(paths, base_dir):
    """Capture supplied files and literal local TeX inputs, never a directory crawl."""
    base_dir = Path(base_dir).resolve()
    seeds = [(Path(p) if Path(p).is_absolute() else base_dir / p).resolve() for p in paths]
    roots = []
    for seed in seeds:
        if seed.suffix.lower() == '.tex':
            try:
                if re.search(r'\\documentclass\b', _uncomment(seed.read_text(encoding='utf-8-sig'))):
                    roots.append(seed.parent)
            except (OSError, UnicodeError):
                pass  # The capture below supplies the actionable source error.
    roots = list(dict.fromkeys(roots)) or ([seeds[0].parent] if seeds else [])

    def root_for(path):
        choices = [root for root in roots if path.is_relative_to(root)]
        return max(choices, key=lambda root: len(root.parts)) if choices else path.parent

    queue = [(path, root_for(path)) for path in seeds]
    captured, unresolved = {}, []
    while queue:
        path, compilation_root = queue.pop(0)
        path = path.resolve()
        if path in captured:
            continue
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise RecordError(f"Cannot capture source {path}: {exc}. Supply the source or correct its path.") from exc
        try:
            display_path = path.relative_to(base_dir).as_posix()
        except ValueError:
            display_path = path.as_posix()
        media = 'application/pdf' if path.suffix.lower() == '.pdf' else 'text/plain'
        captured[path] = {'id': 'file-' + _sha(display_path.encode())[:16], 'path': display_path,
                          'media_type': media, 'sha256': _sha(raw),
                          'content_base64': base64.b64encode(raw).decode('ascii')}
        if path.suffix.lower() not in {'.tex', '.sty', '.cls', '.ltx'}:
            continue
        try:
            text = _uncomment(raw.decode('utf-8-sig'))
        except UnicodeError:
            unresolved.append(f"{display_path}: source discovery requires UTF-8; register relevant inputs explicitly.")
            continue
        for match in re.finditer(r'\\(?:input|include|subfile)\s*\{([^{}]+)\}', text):
            name = match.group(1).strip()
            if '\\' in name or '#' in name:
                unresolved.append(f"{display_path}: dynamic input {name!r}; register the resolved source explicitly.")
                continue
            candidates = [compilation_root / name, path.parent / name]
            candidates = [p if p.suffix else p.with_suffix('.tex') for p in candidates]
            candidate = next((p for p in candidates if p.is_file()), None)
            if candidate is not None:
                queue.append((candidate, compilation_root))
            else:
                unresolved.append(f"{display_path}: unresolved input {name!r}; register or explain the missing source.")
        if re.search(r'\\(?:input|include|subfile)\b(?!\s*\{)|\\(?:import|subimport|inputfrom)\b', text):
            unresolved.append(f"{display_path}: an input form needs manual source registration.")
        # Standard installed TeX packages are not copied. Capture local macro files.
        for match in re.finditer(r'\\(usepackage|RequirePackage|documentclass)\s*(?:\[[^]]*\])?\s*\{([^{}]+)\}', text):
            suffix = '.cls' if match.group(1) == 'documentclass' else '.sty'
            for name in match.group(2).split(','):
                candidates = [compilation_root / (name.strip() + suffix), path.parent / (name.strip() + suffix)]
                candidate = next((p for p in candidates if p.is_file()), None)
                if candidate is not None:
                    queue.append((candidate, compilation_root))
    return list(captured.values()), list(dict.fromkeys(unresolved))


def _binding(locator, file_row, context):
    _fields(locator, (), ('label', 'page', 'start_line', 'end_line'), context)
    if not locator:
        raise RecordError(f"{context}: provide a label, physical PDF page, or line range.")
    for key in ('start_line', 'end_line', 'page'):
        if key in locator and (type(locator[key]) is not int or locator[key] < 1):
            raise RecordError(f"{context}.{key}: expected a positive integer.")
    if ('start_line' in locator) != ('end_line' in locator):
        raise RecordError(f"{context}: start_line and end_line must be supplied together.")
    if 'label' in locator:
        _text(locator['label'], context + '.label')
    excerpt, checks, notes = '', [], []
    text = None
    if file_row and file_row['media_type'] != 'application/pdf':
        text = _source_text(file_row)
    if 'start_line' in locator:
        if text is None:
            raise RecordError(f"{context}: line ranges require a captured UTF-8 text source.")
        start, end = locator['start_line'], locator['end_line']
        lines = text.splitlines()
        if start > end or end > len(lines):
            raise RecordError(f"{context}: lines {start}-{end} are outside or reversed in the captured source ({len(lines)} lines). Re-anchor this passage.")
        excerpt = '\n'.join(lines[start - 1:end])
        checks.append('line_range')
    if 'label' in locator:
        found = text is not None and re.search(r'\\label\s*\{\s*' + re.escape(locator['label']) + r'\s*\}', _uncomment(text))
        if found:
            checks.append('tex_label')
        else:
            notes.append('The entered label has not been matched to a TeX label; printed labels need source comparison.')
    if 'page' in locator:
        if file_row and file_row['media_type'] == 'application/pdf':
            try:
                import io
                from pypdf import PdfReader
                pages = PdfReader(io.BytesIO(_source_bytes(file_row))).pages
                if locator['page'] > len(pages):
                    raise RecordError(f"{context}: PDF page {locator['page']} exceeds its {len(pages)} physical pages.")
                checks.append('pdf_page_bounds')
                if not excerpt:
                    excerpt = pages[locator['page'] - 1].extract_text() or ''
            except RecordError:
                raise
            except Exception as exc:
                notes.append(f"PDF page content could not be mechanically checked ({type(exc).__name__}); inspect the supplied PDF.")
        else:
            notes.append('The entered physical PDF page has not been checked against a captured PDF.')
    if not file_row:
        notes.append('No file content is registered for this locator.')
    verification = {'status': 'checked' if checks and not notes else 'unverified',
                    'method': ', '.join(checks) or 'entered_locator'}
    if notes:
        verification['note'] = ' '.join(notes)
    return excerpt, verification


def _inventory(source_revision, items, anchors, unresolved=()):
    declarations = []
    anchor_map = {a['id']: a for a in anchors}
    names = {kind: kind for kind in KINDS if kind != 'external_result'}
    names.update({'thm': 'theorem', 'lem': 'lemma', 'prop': 'proposition',
                  'cor': 'corollary', 'ass': 'assumption', 'defn': 'definition'})
    unresolved = list(unresolved)
    texts = {}
    for file in source_revision['files']:
        if Path(file['path']).suffix.lower() in {'.tex', '.ltx', '.sty', '.cls'}:
            text = _uncomment(_source_text(file))
            texts[file['id']] = text
            for env, heading in re.findall(r'\\newtheorem\*?\{([^{}]+)\}(?:\[[^]]*\])?\{([^{}]+)\}', text):
                if re.fullmatch(r'(?:remark|example|note|notation|convention)s?', heading.strip(), re.IGNORECASE):
                    continue  # Known minor environments are outside this candidate inventory.
                kind = next((k for k in KINDS if re.search(r'\b' + re.escape(k) + r'\b', heading.lower())), None)
                if kind:
                    names[env] = kind
                else:
                    unresolved.append(f"{file['path']}: theorem environment {env!r} has an unfamiliar heading; compare its declarations manually.")
    for file in source_revision['files']:
        if Path(file['path']).suffix.lower() not in {'.tex', '.ltx'}:
            continue
        text = texts[file['id']]
        for match in re.finditer(r'\\begin\{([^{}]+)\}', text):
            env = match.group(1)
            if env.rstrip('*') not in names:
                continue
            end_match = re.search(r'\\end\{' + re.escape(env) + r'\}', text[match.end():])
            end = match.end() + end_match.end() if end_match else match.end()
            start_line = text.count('\n', 0, match.start()) + 1
            end_line = text.count('\n', 0, end) + 1
            labels = re.findall(r'\\label\s*\{([^{}]+)\}', text[match.start():end])
            matches = []
            for item in items:
                for passage in item['passages']:
                    anchor = anchor_map[passage['anchor_id']]
                    if passage['role'] != 'statement' or anchor.get('file_id') != file['id']:
                        continue
                    loc = anchor['locator']
                    if loc.get('label') in labels or (loc.get('start_line', 0) <= start_line <= loc.get('end_line', -1)):
                        matches.append(item['id'])
            declarations.append({'file_id': file['id'], 'kind': names[env.rstrip('*')],
                                 'start_line': start_line, 'end_line': end_line,
                                 'labels': labels, 'item_ids': sorted(set(matches))})
    return {'method': 'literal_tex_declarations', 'declarations': declarations,
            'unresolved': list(dict.fromkeys(unresolved)), 'excluded': [],
            'note': 'Candidate inventory only. Custom macros, unnumbered prose, and proof meaning require source comparison.'}


def normalize(data, base_dir, extra_files=(), source_root=None):
    """Loss-aware schema-1 import; imports never fabricate completed comparisons."""
    if not isinstance(data, dict):
        raise RecordError('Paper records: expected a JSON object.')
    if data.get('schema_version') == 2:
        if extra_files:
            return refresh_sources(validate_records(data), source_root or base_dir, extra_files=extra_files)
        return validate_records(data)
    import proof_overview
    prepared = proof_overview.validate_data(data, Path(base_dir), allow_cycles=True)
    root = data['source']
    source_objects = [root] + [i['source'] for i in data['items']] + [u['source'] for u in data['uses'] if 'source' in u]
    capture_base = Path(source_root or base_dir).resolve()
    # Seed references are relative to the seed. Captured paths are relative to
    # the manuscript root, which can differ from the output/work directory.
    paths = [(Path(base_dir) / s.get('file', root.get('file'))).resolve()
             for s in source_objects if s.get('file', root.get('file'))] + list(extra_files)
    files, unresolved = _capture(paths, capture_base)
    revision = {'id': _source_digest(files), 'title': root['title'], 'created_at': _now(), 'files': files}
    by_path = {(capture_base / f['path']).resolve(): f for f in files}
    anchors = []

    def anchor(source, identity):
        file_name = source.get('file', root.get('file'))
        file = by_path.get((Path(base_dir) / file_name).resolve()) if file_name else None
        loc = {k: v for k, v in source.items() if k != 'file'}
        excerpt, verification = _binding(loc, file, identity)
        row = {'id': identity, 'source_revision': revision['id'], 'locator': loc,
               'excerpt': excerpt, 'excerpt_hash': _sha(excerpt.encode()), 'verification': verification}
        if file:
            row['file_id'] = file['id']
        anchors.append(row)
        return identity

    items, uses = [], []
    for item in data['items']:
        row = {k: copy.deepcopy(v) for k, v in item.items() if k not in {'source', 'statement'}}
        row['statement'] = {'text': item['statement'], 'form': 'synopsis'}
        row['passages'] = [{'role': 'statement', 'anchor_id': anchor(item['source'], 'anchor-item-' + item['id'])}]
        items.append(row)
    for use, prepared_use in zip(data['uses'], prepared['uses']):
        row = {k: copy.deepcopy(v) for k, v in use.items() if k != 'source'}
        row['id'] = prepared_use['id']
        row.setdefault('type', 'dependency')
        row['evidence_refs'] = [anchor(use['source'], 'anchor-' + row['id'])] if 'source' in use else []
        uses.append(row)
    result = {'schema_version': 2, 'title': data['title'], 'scope': data['scope'],
              'source_revision': revision, 'anchors': anchors, 'items': items, 'uses': uses,
              'observations': [], 'inventory': _inventory(revision, items, anchors, unresolved)}
    if 'main_items' in data:
        result['main_items'] = copy.deepcopy(data['main_items'])
    return validate_records(result)


def validate_records(data):
    """Validate content, captured evidence and references; allow cyclic mappings."""
    _fields(data, ('schema_version', 'title', 'scope', 'source_revision', 'anchors', 'items', 'uses', 'observations'),
            ('snapshot_id', 'inventory', 'main_items'), 'Paper records')
    if type(data['schema_version']) is not int or data['schema_version'] != 2:
        raise RecordError('Paper records: supported schema_version is 2.')
    result = copy.deepcopy(data)
    for key in ('title', 'scope'):
        _text(result[key], key)
    source = result['source_revision']
    _fields(source, ('id', 'title', 'created_at', 'files'), (), 'Source revision')
    _text(source['title'], 'Source revision.title')
    _text(source['created_at'], 'Source revision.created_at')
    file_map, paths = {}, set()
    for row in _rows(source['files'], 'Source files'):
        _fields(row, ('id', 'path', 'media_type', 'sha256', 'content_base64'), (), 'Source file')
        _id(row['id'], 'Source file.id')
        for key in ('path', 'media_type', 'sha256'):
            _text(row[key], 'Source file.' + key)
        if row['media_type'] not in {'text/plain', 'application/pdf'}:
            raise RecordError(f"Source {row['path']}: unsupported media_type.")
        if row['id'] in file_map or row['path'] in paths:
            raise RecordError(f"Source {row['path']}: duplicate file identity or path.")
        _source_bytes(row)
        file_map[row['id']] = row
        paths.add(row['path'])
    if source['id'] != _source_digest(source['files']):
        raise RecordError('Source revision digest does not match its manifest. Register the changed source as a new revision.')
    anchor_map = {}
    for row in _rows(result['anchors'], 'Anchors'):
        _fields(row, ('id', 'source_revision', 'locator', 'excerpt', 'excerpt_hash', 'verification'), ('file_id',), 'Anchor')
        _id(row['id'], 'Anchor.id')
        if row['id'] in anchor_map:
            raise RecordError(f"Anchor {row['id']}: duplicate identity.")
        if row['source_revision'] != source['id']:
            raise RecordError(f"Anchor {row['id']}: binding belongs to a different source revision. Re-anchor it explicitly.")
        if 'file_id' in row:
            _text(row['file_id'], 'Anchor file_id')
        file = file_map.get(row.get('file_id'))
        if 'file_id' in row and not file:
            raise RecordError(f"Anchor {row['id']}: unknown file {row['file_id']!r}.")
        _text(row['excerpt'], 'Anchor excerpt', empty=True)
        if row['excerpt_hash'] != _sha(row['excerpt'].encode()):
            raise RecordError(f"Anchor {row['id']}: excerpt hash mismatch.")
        excerpt, checked = _binding(row['locator'], file, row['id'])
        # PDF extraction can vary between shared reader versions. Retain the
        # captured transcription; exact text ranges remain mechanically bound.
        if 'start_line' in row['locator'] and row['excerpt'] != excerpt:
            raise RecordError(f"Anchor {row['id']}: excerpt differs from the captured line range.")
        _fields(row['verification'], ('status', 'method'), ('note',), 'Anchor verification')
        _text(row['verification']['status'], 'Anchor verification.status')
        if row['verification']['status'] not in {'checked', 'unverified'}:
            raise RecordError(f"Anchor {row['id']}: unsupported verification status.")
        _text(row['verification']['method'], 'Anchor verification.method')
        if 'note' in row['verification']:
            _text(row['verification']['note'], 'Anchor verification.note')
        if row['verification']['status'] == 'checked' and checked['status'] != 'checked':
            raise RecordError(f"Anchor {row['id']}: locator is marked checked without reproducible locator evidence.")
        anchor_map[row['id']] = row
    item_map = {}
    for row in _rows(result['items'], 'Items'):
        _fields(row, ('id', 'kind', 'label', 'caption', 'statement', 'passages'), ('aliases', 'uncertainty'), 'Item')
        _id(row['id'], 'Item.id')
        for key in ('kind', 'label', 'caption'):
            _text(row[key], 'Item.' + key)
        if row['id'] in item_map or row['kind'] not in KINDS:
            raise RecordError(f"Item {row['id']}: duplicate identity or unsupported mathematical kind.")
        _fields(row['statement'], ('text', 'form'), (), row['label'] + ' statement')
        _text(row['statement']['text'], row['label'] + ' statement.text')
        _text(row['statement']['form'], row['label'] + ' statement.form')
        if row['statement']['form'] not in {'verbatim', 'transcription', 'synopsis'}:
            raise RecordError(f"{row['label']}: statement form must be verbatim, transcription, or synopsis.")
        passages = _rows(row['passages'], row['label'] + ' passages')
        if not passages:
            raise RecordError(f"{row['label']}: retain at least one source passage or explicitly unverified locator.")
        seen = set()
        for passage in passages:
            _fields(passage, ('role', 'anchor_id'), (), 'Passage link')
            _text(passage['role'], 'Passage role')
            _text(passage['anchor_id'], 'Passage anchor_id')
            if passage['role'] not in {'statement', 'proof', 'definition', 'evidence'} or passage['anchor_id'] not in anchor_map:
                raise RecordError(f"{row['label']}: passage has an unknown role or anchor.")
            pair = passage['role'], passage['anchor_id']
            if pair in seen:
                raise RecordError(f"{row['label']}: duplicate passage link.")
            seen.add(pair)
        aliases = _rows(row.get('aliases', []), row['label'] + ' aliases')
        for alias in aliases:
            _text(alias, row['label'] + ' alias')
        if len(set(aliases)) != len(aliases):
            raise RecordError(f"{row['label']}: duplicate alias.")
        if 'uncertainty' in row:
            _text(row['uncertainty'], row['label'] + ' uncertainty')
        item_map[row['id']] = row
    if not item_map:
        raise RecordError('Paper records: retain at least one major item.')
    use_map = {}
    for row in _rows(result['uses'], 'Uses'):
        _fields(row, ('id', 'from', 'to', 'type', 'reason', 'evidence_refs'), ('regime', 'uncertainty'), 'Use')
        _id(row['id'], 'Use.id')
        if row['id'] in use_map:
            raise RecordError(f"Use {row['id']}: duplicate identity. Distinct uses need distinct IDs.")
        for key in ('from', 'to'):
            _text(row[key], 'Use.' + key)
            if row[key] not in item_map:
                raise RecordError(f"Use {row['id']}: unknown {key} item {row[key]!r}. Correct the reference; do not substitute another result without evidence.")
        _text(row['type'], 'Use.type')
        if row['type'] not in USE_TYPES:
            raise RecordError(f"Use {row['id']}: unsupported use type.")
        _text(row['reason'], 'Use.reason')
        for key in ('regime', 'uncertainty'):
            if key in row:
                _text(row[key], 'Use.' + key)
        refs = _rows(row['evidence_refs'], 'Use evidence_refs')
        if any(not isinstance(ref, str) or ref not in anchor_map for ref in refs) or len(set(refs)) != len(refs):
            raise RecordError(f"Use {row['id']}: evidence references must be distinct existing anchor IDs.")
        use_map[row['id']] = row
    if 'main_items' in result:
        main = _rows(result['main_items'], 'main_items')
        if not main or any(not isinstance(k, str) or k not in item_map for k in main) or len(set(main)) != len(main):
            raise RecordError('main_items: choose distinct existing item IDs, or omit the field.')
    observation_ids = set()
    observation_map = {}
    for obs in _rows(result['observations'], 'Observations'):
        _fields(obs, ('id', 'target', 'input_snapshot', 'result', 'note', 'reviewer', 'created_at'), ('carried_from',), 'Observation')
        _id(obs['id'], 'Observation.id')
        if obs['id'] in observation_ids:
            raise RecordError('Duplicate observation identity.')
        observation_ids.add(obs['id'])
        _fields(obs['target'], ('collection', 'id'), (), 'Observation.target')
        _text(obs['target']['collection'], 'Observation target.collection')
        if obs['target']['collection'] not in {'items', 'uses'}:
            raise RecordError('Observation: target collection must be items or uses.')
        _id(obs['target']['id'], 'Observation target.id')
        for key in ('input_snapshot', 'reviewer', 'created_at'):
            _text(obs[key], 'Observation.' + key)
        _text(obs['note'], 'Observation.note', empty=True)
        _text(obs['result'], 'Observation.result')
        if obs['result'] not in {'matched', 'needs_attention'}:
            raise RecordError('Observation result must be matched or needs_attention; neither is a proof verdict.')
        if 'carried_from' in obs:
            _id(obs['carried_from'], 'Observation carried_from')
            prior = observation_map.get(obs['carried_from'])
            if not prior or prior['target'] != obs['target'] or prior['result'] != 'matched' or obs['result'] != 'matched':
                raise RecordError('A carried comparison must reference an earlier matched observation for the same target.')
        observation_map[obs['id']] = obs
    if 'inventory' in result:
        _fields(result['inventory'], ('method', 'declarations', 'unresolved', 'excluded', 'note'), (), 'Inventory')
        for key in ('method', 'note'):
            _text(result['inventory'][key], 'Inventory.' + key)
        for key in ('declarations', 'unresolved', 'excluded'):
            _rows(result['inventory'][key], 'Inventory.' + key)
        for note in result['inventory']['unresolved'] + result['inventory']['excluded']:
            _text(note, 'Inventory limitation')
        for decl in result['inventory']['declarations']:
            _fields(decl, ('file_id', 'kind', 'start_line', 'end_line', 'labels', 'item_ids'), (), 'Inventory declaration')
            _text(decl['file_id'], 'Inventory file_id')
            _text(decl['kind'], 'Inventory kind')
            if decl['file_id'] not in file_map or decl['kind'] not in KINDS:
                raise RecordError('Inventory declaration: unknown source file or kind.')
            _binding({'start_line': decl['start_line'], 'end_line': decl['end_line']}, file_map[decl['file_id']], 'Inventory declaration')
            for field in ('labels', 'item_ids'):
                for val in _rows(decl[field], 'Inventory ' + field):
                    _text(val, 'Inventory ' + field)
            if any(val not in item_map for val in decl['item_ids']):
                raise RecordError('Inventory declaration references an unknown item.')
    identity = snapshot_digest(result)
    if 'snapshot_id' in result and result['snapshot_id'] != identity:
        raise RecordError('Dataset snapshot digest does not match its content. Apply an explicit edit batch or remove snapshot_id from a deliberately edited standalone JSON dataset.')
    result['snapshot_id'] = identity
    return result


def _relocate_exact(anchor, file):
    """Move a line locator only on an exact unique whole-line excerpt match."""
    locator = anchor['locator']
    if not file or file['media_type'] == 'application/pdf' or 'start_line' not in locator or not anchor['excerpt'].strip():
        return
    lines = _source_text(file).splitlines()
    prior = anchor['excerpt'].splitlines()
    start, end = locator['start_line'], locator['end_line']
    if lines[start - 1:end] == prior:
        return
    matches = [index for index in range(len(lines) - len(prior) + 1) if lines[index:index + len(prior)] == prior]
    if len(matches) > 1:
        raise RecordError(f"Anchor {anchor['id']}: the prior excerpt occurs at several locations. Supply an explicit locator with refresh --anchors; no location was guessed.")
    if matches:
        locator.update(start_line=matches[0] + 1, end_line=matches[0] + len(prior))


def refresh_sources(data, base_dir, extra_files=(), anchor_locations=None, relocate_exact=False, file_map=None):
    original = validate_records(data)
    result = copy.deepcopy(original)
    old_source = original['source_revision']
    renames = {} if file_map is None else file_map
    if not isinstance(renames, dict) or set(renames) - {f['id'] for f in old_source['files']}:
        raise RecordError('file_map must map existing source file IDs to explicit new paths, or null for removed sources.')
    paths, preserved_ids = [], {}
    for old_file in old_source['files']:
        path = renames.get(old_file['id'], old_file['path'])
        if path is None:
            continue
        _text(path, 'Source file mapping')
        resolved = (Path(base_dir) / path).resolve()
        if resolved in preserved_ids:
            raise RecordError('Two existing source files map to the same path. Resolve their identities and anchors explicitly.')
        preserved_ids[resolved] = old_file['id']
        paths.append(path)
    try:
        files, unresolved = _capture(paths + list(extra_files), base_dir)
    except RecordError as exc:
        raise RecordError(f"{exc} If a source was renamed or removed, use refresh --file-map with its registered file ID. The existing snapshot was preserved.") from exc
    for file in files:
        file['id'] = preserved_ids.get((Path(base_dir) / file['path']).resolve(), file['id'])
    identity = _source_digest(files)
    result['source_revision'] = {'id': identity, 'title': old_source['title'],
                                 'created_at': old_source['created_at'] if identity == old_source['id'] else _now(), 'files': files}
    new_by_id = {f['id']: f for f in files}
    locations = anchor_locations or {}
    if not isinstance(locations, dict) or set(locations) - {a['id'] for a in result['anchors']}:
        raise RecordError('Refresh anchor locations must map existing anchor IDs to locator objects.')
    for anchor in result['anchors']:
        if anchor['id'] in locations:
            edit = locations[anchor['id']]
            _fields(edit, ('locator',), ('file_id',), 'Refresh anchor ' + anchor['id'])
            anchor['locator'] = copy.deepcopy(edit['locator'])
            if 'file_id' in edit:
                _text(edit['file_id'], 'Refresh anchor file_id')
                anchor['file_id'] = edit['file_id']
        file = new_by_id.get(anchor.get('file_id'))
        if anchor.get('file_id') and file is None:
            raise RecordError(f"Anchor {anchor['id']}: its source was removed. Remove the unused anchor or rebind it explicitly with --anchors before refreshing.")
        try:
            if relocate_exact and anchor['id'] not in locations:
                _relocate_exact(anchor, file)
            excerpt, verification = _binding(anchor['locator'], file, anchor['id'])
        except RecordError as exc:
            raise RecordError(f"{exc} Supply corrected locations using refresh --anchors reanchors.json; the existing database snapshot is preserved.") from exc
        anchor.update(source_revision=identity, excerpt=excerpt, excerpt_hash=_sha(excerpt.encode()), verification=verification)
    result['inventory'] = _inventory(result['source_revision'], result['items'], result['anchors'], unresolved)
    result['inventory']['excluded'] = original.get('inventory', {}).get('excluded', [])
    result.pop('snapshot_id', None)
    return validate_records(result)


def make_anchor(data, locator, file_id=None, identity=None):
    """Generate source bookkeeping for a proposed passage in a captured revision."""
    source = data['source_revision']
    file = next((f for f in source['files'] if f['id'] == file_id), None)
    if file_id is not None and file is None:
        raise RecordError(f"Unknown source file {file_id!r}; register it before anchoring a passage.")
    identity = _id(identity or 'anchor-' + uuid.uuid4().hex, 'Anchor.id')
    excerpt, verification = _binding(locator, file, identity)
    anchor = {'id': identity, 'source_revision': source['id'], 'locator': copy.deepcopy(locator),
              'excerpt': excerpt, 'excerpt_hash': _sha(excerpt.encode()), 'verification': verification}
    if file_id is not None:
        anchor['file_id'] = file_id
    return anchor


def update_inventory(data):
    """Recompute declaration links after item edits, retaining stated limitations."""
    result = copy.deepcopy(data)
    prior = result.pop('inventory', {})
    result.pop('snapshot_id', None)
    result = validate_records(result)
    result['inventory'] = _inventory(result['source_revision'], result['items'], result['anchors'], prior.get('unresolved', []))
    result['inventory']['excluded'] = prior.get('excluded', [])
    result.pop('snapshot_id', None)
    return validate_records(result)


def target_digest(data, collection, identity):
    _text(collection, 'Comparison collection')
    if collection not in {'items', 'uses'}:
        raise RecordError('Comparison target collection must be items or uses.')
    records = {row['id']: row for row in data[collection]}
    if identity not in records:
        raise RecordError(f"Comparison target {collection}/{identity} is absent.")
    target = records[identity]
    items = {row['id']: row for row in data['items']}
    context = {'target': target, 'source_revision': data['source_revision']['id']}
    if collection == 'items':
        uses = [u for u in data['uses'] if u['to'] == identity]
        relevant_items = [target] + [items[u['from']] for u in uses]
    else:
        uses, relevant_items = [target], [items[target['from']], items[target['to']]]
    anchors = {p['anchor_id'] for i in relevant_items for p in i['passages']}
    anchors.update(a for u in uses for a in u['evidence_refs'])
    context.update(uses=uses, items=relevant_items, anchors=[a for a in data['anchors'] if a['id'] in anchors])
    return _digest(context)


def make_observations(data, requests, reviewer, note='', result='matched'):
    data = validate_records(data)
    _text(reviewer, 'Reviewer')
    _text(note, 'Comparison note', empty=True)
    _text(result, 'Comparison result')
    if result not in {'matched', 'needs_attention'}:
        raise RecordError('Comparison result must be matched or needs_attention.')
    observations = []
    for request in _rows(requests, 'Comparison targets'):
        _fields(request, ('collection', 'id'), (), 'Comparison target')
        observations.append({'id': 'comparison-' + uuid.uuid4().hex, 'target': copy.deepcopy(request),
                             'input_snapshot': target_digest(data, request['collection'], request['id']),
                             'result': result, 'note': note, 'reviewer': reviewer, 'created_at': _now()})
    return observations


def applicable_observations(data):
    """Newest observation for the exact input, falling back to stale history."""
    history = {}
    for observation in data['observations']:
        key = observation['target']['collection'], observation['target']['id']
        history.setdefault(key, []).append(observation)
    applicable = []
    for collection in ('items', 'uses'):
        for row in data[collection]:
            candidates = history.get((collection, row['id']), [])
            if not candidates:
                continue
            identity = target_digest(data, collection, row['id'])
            applicable.append(next((o for o in reversed(candidates) if o['input_snapshot'] == identity), candidates[-1]))
    return applicable


def comparison_status(data):
    latest = {(o['target']['collection'], o['target']['id']): o for o in applicable_observations(data)}
    counts = {'matched': 0, 'needs_attention': 0, 'unreviewed': 0, 'stale': 0,
              'total': len(data['items']) + len(data['uses'])}
    for collection in ('items', 'uses'):
        for row in data[collection]:
            observation = latest.get((collection, row['id']))
            status = 'unreviewed' if observation is None else (
                'stale' if observation['input_snapshot'] != target_digest(data, collection, row['id']) else observation['result'])
            counts[status] += 1
    return {'status': 'complete' if counts['matched'] == counts['total'] else 'incomplete', **counts}


def source_status(data, base_dir):
    statuses = []
    for row in data['source_revision']['files']:
        try:
            statuses.append('current' if _sha((Path(base_dir) / row['path']).read_bytes()) == row['sha256'] else 'historical_changed')
        except OSError:
            statuses.append('historical_unavailable')
    return next((state for state in ('historical_changed', 'historical_unavailable') if state in statuses),
                'current' if statuses else 'unregistered')


def prepare_records(data, base_dir):
    data = validate_records(data)
    files = {f['id']: f for f in data['source_revision']['files']}
    anchors = {a['id']: a for a in data['anchors']}
    prepared = {'schema_version': 2, 'title': data['title'], 'scope': data['scope'],
                'source': {'title': data['source_revision']['title']}, 'items': [], 'uses': [], 'warnings': []}

    def passage(identity, role):
        anchor = anchors[identity]
        loc = anchor['locator']
        display = []
        if loc.get('label'):
            display.append(loc['label'])
        if loc.get('page'):
            display.append(f"PDF p. {loc['page']}")
        if anchor.get('file_id'):
            display.append(Path(files[anchor['file_id']]['path']).name)
        if loc.get('start_line'):
            display.append(f"lines {loc['start_line']}-{loc['end_line']}")
        return {'role': role, 'anchor_id': identity, 'source_display': ' · '.join(display),
                'source_excerpt': anchor['excerpt'], 'verification': anchor['verification'],
                'source_file': files[anchor['file_id']]['path'] if anchor.get('file_id') else None}

    for item in data['items']:
        row = copy.deepcopy(item)
        row['statement_form'] = row['statement']['form']
        row['statement'] = row['statement']['text']
        row['statement_html'] = render_text(row['statement'])
        row['source_passages'] = [passage(p['anchor_id'], p['role']) for p in row['passages']]
        primary = next((p for p in row['source_passages'] if p['role'] == 'statement'), row['source_passages'][0])
        row.update({k: primary[k] for k in ('source_display', 'source_excerpt')})
        prepared['items'].append(row)
    for use in data['uses']:
        row = copy.deepcopy(use)
        row['reason_html'] = render_text(row['reason'])
        row['source_passages'] = [passage(a, 'evidence') for a in row['evidence_refs']]
        if row['source_passages']:
            row.update({k: row['source_passages'][0][k] for k in ('source_display', 'source_excerpt')})
        prepared['uses'].append(row)
    if 'main_items' in data:
        prepared['main_items'] = data['main_items'][:]
    # Layout constraints do not invalidate the paper dataset.
    incoming = {i['id']: 0 for i in data['items']}
    outgoing = {key: [] for key in incoming}
    for use in data['uses']:
        incoming[use['to']] += 1
        outgoing[use['from']].append(use['to'])
    ready = [key for key in incoming if incoming[key] == 0]
    for key in ready:
        for target in outgoing[key]:
            incoming[target] -= 1
            if not incoming[target]:
                ready.append(target)
    prepared['graph_mode'] = 'dag' if len(ready) == len(incoming) else 'index'
    if prepared['graph_mode'] == 'index':
        prepared['warnings'].append('The combined dependency map contains a cycle. All items and uses are retained in the index; this layout limitation does not establish a circular proof. Compare the directions and alternative regimes with the manuscript.')
    freshness = source_status(data, base_dir)
    if freshness != 'current':
        prepared['warnings'].append({'historical_changed': 'The live manuscript differs from this captured source version. This overview displays historical records; refresh and compare them before calling it current.',
                                     'historical_unavailable': 'Some live source files are unavailable. This overview uses its captured historical source version.',
                                     'unregistered': 'No source files are registered. Entered locators and completeness need source comparison.'}[freshness])
    status = comparison_status(data)
    if status['status'] != 'complete':
        prepared['warnings'].append(f"Source comparison is incomplete: {status['unreviewed']} records unreviewed, {status['stale']} comparisons stale, {status['needs_attention']} needing attention. These are overview comparisons, not proof verdicts.")
    unverified = sum(a['verification']['status'] != 'checked' for a in data['anchors'])
    if unverified:
        prepared['warnings'].append(f"{unverified} locators include labels or pages that were not mechanically verified. See passage details for checked locations and limitations; source comparisons are reported separately.")
    inventory = data.get('inventory', {})
    missing = sum(not d['item_ids'] for d in inventory.get('declarations', []))
    if missing:
        prepared['warnings'].append(f"{missing} detected declarations are not matched to overview items. Check the selected scope and candidate inventory.")
    prepared['warnings'].extend(inventory.get('unresolved', []))
    prepared['warnings'].extend('Scope exclusion: ' + note for note in inventory.get('excluded', []))
    for row in prepared['items'] + prepared['uses']:
        if 'class="math-fallback"' in row.get('statement_html', row.get('reason_html', '')):
            prepared['warnings'].append(f"{row.get('label', 'A dependency reason')}: some LaTeX could not be typeset; the original expression remains visibly labeled.")
    prepared['build_context'] = {'input_snapshot': data['snapshot_id'], 'source_revision': data['source_revision']['id'],
                                 'source_status': freshness, 'source_comparison': status,
                                 'mathematical_assessment': 'not_performed'}
    prepared['inventory'] = {k: v for k, v in inventory.items() if k != 'declarations'}
    return prepared
