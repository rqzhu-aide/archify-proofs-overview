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

MAJOR_KINDS = ("assumption", "definition", "lemma", "proposition", "theorem", "corollary", "external_result")
INTERMEDIATE_KINDS = ("equation", "claim", "derivation")
KINDS = frozenset(MAJOR_KINDS + INTERMEDIATE_KINDS)  # Record validation only; inventories and displays use MAJOR_KINDS.
USE_TYPES = {"dependency", "definition", "proof_argument"}

# Heading words normalize to a major kind in declaration order; a
# condition-style declaration is an assumption. This restates the audit
# core's KIND_WORDS idea locally so the scanner needs no cross-skill import.
HEADING_WORDS = tuple((kind, kind) for kind in MAJOR_KINDS if kind != 'external_result') + (('condition', 'assumption'),)


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


_TEX_SUFFIXES = {'.tex', '.ltx', '.sty', '.cls'}
_TEX_MARKER_RE = re.compile(r'\\(?:documentclass|newtheorem\*?)\b|\\begin\s*\{document\}')


def _is_tex_content(text, path):
    """TeX by suffix, or a conservative manuscript marker in the decoded content."""
    if Path(path).suffix.lower() in _TEX_SUFFIXES:
        return True
    return bool(_TEX_MARKER_RE.search(text))


def _tex_by_content_note(path):
    return (path + ": treated as TeX by content: a manuscript marker "
            "(\\documentclass, \\newtheorem, or \\begin{document}) occurs under a non-TeX "
            "file extension; declaration and citation scans include it.")


_ENV_TOKEN_RE = re.compile(r'\\(begin|end)\s*\{([^{}]+)\}')


def _environment_spans(text):
    """Pair \\begin/\\end tokens with a stack; unmatched tokens pair with nothing.

    Ported from the audit core's declaration scanner (paper_core sources.py) so
    nested environments pair with their own \\end instead of the next one
    sharing the name. Returns (env, begin start/end, end start/end) offset
    tuples sorted by begin offset.
    """
    stack, spans = [], []
    for match in _ENV_TOKEN_RE.finditer(text):
        kind, env = match.group(1), match.group(2)
        if kind == 'begin':
            stack.append((env, match.start(), match.end()))
            continue
        for depth in range(len(stack) - 1, -1, -1):
            if stack[depth][0] == env:
                _, bstart, bend = stack[depth]
                del stack[depth:]
                spans.append((env, bstart, bend, match.start(), match.end()))
                break
    spans.sort(key=lambda span: span[1])
    return spans


def _line_of(text, offset):
    return text.count('\n', 0, offset) + 1


def _capture(paths, base_dir):
    """Capture supplied files and literal local TeX inputs, never a directory crawl."""
    base_dir = Path(base_dir).resolve()
    seeds = [(Path(p) if Path(p).is_absolute() else base_dir / p).resolve() for p in paths]
    roots = []
    for seed in seeds:
        if seed.suffix.lower() == '.pdf':
            continue
        try:
            seed_text = _uncomment(seed.read_text(encoding='utf-8-sig'))
        except (OSError, UnicodeError):
            continue  # The capture below supplies the actionable source error.
        if _is_tex_content(seed_text, seed) and re.search(r'\\documentclass\b', seed_text):
            roots.append(seed.parent)
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
        if path.suffix.lower() == '.pdf':
            continue
        try:
            text = _uncomment(raw.decode('utf-8-sig'))
        except UnicodeError:
            if path.suffix.lower() in _TEX_SUFFIXES:
                unresolved.append(f"{display_path}: source discovery requires UTF-8; register relevant inputs explicitly.")
            continue
        if not _is_tex_content(text, path):
            continue
        if path.suffix.lower() not in _TEX_SUFFIXES:
            unresolved.append(_tex_by_content_note(display_path))
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


def _declared_kinds(source_revision, unresolved):
    """Environment-name knowledge shared by the inventory and citation scans.

    Returns (names, texts, sniffed): the theorem-like environment map, the
    uncommented text of every TeX-treated file, and the paths treated as TeX
    by content alone. TeX-suffix files fail loudly on undecodable bytes; other
    files are scanned only when a conservative manuscript marker is present.
    """
    names = {kind: kind for kind in MAJOR_KINDS if kind != 'external_result'}
    names.update({'thm': 'theorem', 'lem': 'lemma', 'prop': 'proposition',
                  'cor': 'corollary', 'ass': 'assumption', 'defn': 'definition'})
    texts, sniffed = {}, []
    for file in source_revision['files']:
        if Path(file['path']).suffix.lower() in _TEX_SUFFIXES:
            text = _uncomment(_source_text(file))
        elif file['media_type'] == 'application/pdf':
            continue
        else:
            try:
                decoded = _source_bytes(file).decode('utf-8-sig')
            except UnicodeError:
                continue  # Not UTF-8 text; nothing scanable. Hash problems still raise.
            if not _is_tex_content(decoded, file['path']):
                continue
            sniffed.append(file['path'])
            unresolved.append(_tex_by_content_note(file['path']))
            text = _uncomment(decoded)
        texts[file['id']] = text
        for env, heading in re.findall(r'\\newtheorem\*?\{([^{}]+)\}(?:\[[^]]*\])?\{([^{}]+)\}', text):
            if re.fullmatch(r'(?:remark|example|note|notation|convention)s?', heading.strip(), re.IGNORECASE):
                continue  # Known minor environments are outside this candidate inventory.
            kind = next((kind for word, kind in HEADING_WORDS
                         if re.search(r'\b' + re.escape(word) + r's?\b', heading.lower())), None)
            if kind:
                names[env] = kind
            else:
                unresolved.append(f"{file['path']}: theorem environment {env!r} has an unfamiliar heading; compare its declarations manually.")
    return names, texts, sniffed


def _statement_anchor_rows(items, anchors, file_id, label_map, labels, start_line):
    """Rows owning a declaration span: its labels' rows, else the statement anchor enclosing its first line."""
    rows = {label_map[label] for label in labels if label in label_map}
    if rows:
        return rows
    matched = set()
    for item in items:
        for passage in item['passages']:
            anchor = anchors[passage['anchor_id']]
            if passage['role'] != 'statement' or anchor.get('file_id') != file_id:
                continue
            loc = anchor['locator']
            if loc.get('start_line', 0) <= start_line <= loc.get('end_line', -1):
                matched.add(item['id'])
    return matched


def _inventory(source_revision, items, anchors, unresolved=()):
    declarations = []
    anchor_map = {a['id']: a for a in anchors}
    unresolved = list(unresolved)
    names, texts, _sniffed = _declared_kinds(source_revision, unresolved)
    for file in source_revision['files']:
        # Declarations live in documents, not in class/style files: TeX-suffixed
        # documents plus any file treated as TeX by content.
        if file['id'] not in texts or Path(file['path']).suffix.lower() in {'.sty', '.cls'}:
            continue
        text = texts[file['id']]
        for env, bstart, bend, estart, eend in _environment_spans(text):
            if env.rstrip('*') not in names:
                continue
            start_line = _line_of(text, bstart)
            end_line = _line_of(text, eend)
            labels = re.findall(r'\\label\s*\{([^{}]+)\}', text[bstart:eend])
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


_REF_RE = re.compile(r'\\(eqref|autoref|[cC]ref|ref)\s*\{([^{}]+)\}')
_SECTION_RE = re.compile(r'\\(section|subsection|subsubsection)\s*\*?\s*(?:\[[^\]]*\])?\s*\{')
_SECTION_LEVELS = {'section': 1, 'subsection': 2, 'subsubsection': 3}
_PROOF_OF_RE = re.compile(r'proof\s+of\b', re.IGNORECASE)
_HEADING_REF_RE = re.compile(r'\\ref\s*\{([^{}]+)\}')
_PROOF_LABEL_RE = re.compile(r'\\label\s*\{proof[:-]([^{}]+)\}')


def _braced_end(text, open_index):
    """Offset just past the brace group opening at open_index; None if unbalanced."""
    depth = 0
    for index in range(open_index, len(text)):
        char = text[index]
        if char == '{' and (index == 0 or text[index - 1] != '\\'):
            depth += 1
        elif char == '}' and text[index - 1] != '\\':
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _label_suffix(label):
    """A label minus its own kind prefix: thm:fixed_point -> fixed_point."""
    for separator in (':', '-'):
        head, found, tail = label.partition(separator)
        if found and head and tail:
            return tail
    return label


def _proof_section_spans(text, label_map, suffix_map):
    """Proof sections attributed by heading conventions, in priority order.

    A sectioning command (starred or not) whose title carries 'proof of'
    followed by \\ref{key} attributes the span through the next same-or-higher
    heading to key's row; failing that, a \\label{proof:suffix} (or proof-
    suffix) on the heading line attributes it to the row whose label reduces
    to suffix. Recognized but unresolvable headings still yield ownerless
    spans, so occurrences inside classify as proof context. Returns (start,
    end, owner, 'proof') tuples.
    """
    headings = []
    for match in _SECTION_RE.finditer(text):
        close = _braced_end(text, match.end() - 1)
        if close is not None:
            headings.append((_SECTION_LEVELS[match.group(1)], match.start(),
                             text[match.end():close - 1], close))
    spans = []
    for index, (level, start, title, title_end) in enumerate(headings):
        end = len(text)
        for other_level, other_start, _title, _end in headings[index + 1:]:
            if other_level <= level:
                end = other_start
                break
        document_end = text.find('\\end{document}', title_end)
        if document_end != -1:
            end = min(end, document_end)
        owner, proofish = None, False
        proof_of = _PROOF_OF_RE.search(title)
        if proof_of:
            proofish = True
            ref = _HEADING_REF_RE.search(title, proof_of.start())
            if ref:
                owner = label_map.get(ref.group(1).strip())
        if owner is None:
            line_end = text.find('\n', title_end)
            marker = _PROOF_LABEL_RE.search(text[start:line_end if line_end != -1 else len(text)])
            if marker:
                proofish = True
                owner = suffix_map.get(marker.group(1).strip())
        if owner is not None or proofish:
            spans.append((start, end, owner, 'proof'))
    return spans


def citation_summary(data):
    """Compact scan summary for validate/render receipts; the full report lists lines."""
    report = citation_candidates(data)
    if not report['coverage']['applicable']:
        return 'not applicable (no TeX sources captured)'
    counts = report['counts']
    return {'pairs': counts['pairs'],
            'not_mechanically_matchable': counts['not_mechanically_matchable'],
            'missing_uses': counts['missing_uses'], 'unsupported_uses': counts['unsupported_uses'],
            'unmatched_labels': counts['unmatched_labels'],
            'attributed': report['attribution']['attributed'],
            'unattributed': report['attribution']['unattributed']}


def citation_candidates(data):
    """\\ref-family citation evidence for the rows of one captured snapshot.

    The label-to-row map comes from statement-passage anchors carrying TeX
    labels; occurrence contexts come from environment spans in the captured
    bytes (statement/proof of an attributed row, else narrative). Proof spans
    are attributed by the nearest enclosing 'proof of' section heading, then
    by a proof:<suffix> label convention, then by whitespace adjacency to a
    declaration. The lists propose reviews; no citation is itself a recorded
    use.
    """
    data = validate_records(data)
    items = data['items']
    anchors = {a['id']: a for a in data['anchors']}
    claimed = {}
    for item in items:
        for passage in item['passages']:
            if passage['role'] != 'statement':
                continue
            label = anchors[passage['anchor_id']]['locator'].get('label')
            if label:
                claimed.setdefault(label, set()).add(item['id'])
    label_map = {label: next(iter(rows)) for label, rows in claimed.items() if len(rows) == 1}
    collisions = [{'label': label, 'rows': sorted(rows)}
                  for label, rows in sorted(claimed.items()) if len(rows) > 1]
    suffix_rows = {}
    for label, row in label_map.items():
        suffix_rows.setdefault(_label_suffix(label), set()).add(row)
    suffix_map = {suffix: next(iter(rows)) for suffix, rows in suffix_rows.items() if len(rows) == 1}
    matchable = set(label_map.values())
    order = {row['id']: index for index, row in enumerate(items)}
    # Authored proof-passage ranges can locate prose arguments whose headings
    # the source parser does not recognize. Declaration contexts and statement
    # ranges always outrank an enclosing proof range.
    passage_lines = {}
    for item in items:
        for passage in item['passages']:
            anchor = anchors[passage['anchor_id']]
            start = anchor['locator'].get('start_line')
            if anchor.get('file_id') is None or start is None or passage['role'] not in ('statement', 'proof'):
                continue
            passage_lines.setdefault((anchor['file_id'], passage['role']), []).append(
                (start, anchor['locator']['end_line'], item['id']))
    names, texts, sniffed = _declared_kinds(data['source_revision'], [])
    text_files_total = sum(1 for file in data['source_revision']['files'] if file['media_type'] != 'application/pdf')
    coverage = {'files_scanned': len(texts), 'text_files_total': text_files_total,
                'applicable': bool(texts), 'tex_by_content': sniffed}
    if not coverage['applicable']:
        return {'snapshot_id': data['snapshot_id'], 'source_revision': data['source_revision']['id'],
                'note': ('Not applicable: no captured source is treated as TeX '
                         f"(0 of {text_files_total} captured text files; PDFs carry no \\ref commands). "
                         'Review the recorded rows through get packets and compare batches, and disclose the '
                         'missing citation evidence as a coverage limit. Register the manuscript TeX sources for '
                         'citation evidence; the optional scaffold/reconcile edge audit also works from the '
                         'recorded rows alone and needs no TeX.'),
                'coverage': coverage, 'attribution': None, 'counts': None, 'pairs': None, 'missing_uses': None,
                'unsupported_uses': None, 'unattributed_occurrences': [], 'unmatched_labels': [],
                'not_mechanically_matchable': [{'id': row['id'], 'kind': row['kind'], 'label': row['label']}
                                               for row in items if row['id'] not in matchable],
                'label_collisions': collisions}
    pairs, unattributed, unmatched = {}, [], {}
    for file in data['source_revision']['files']:
        text = texts.get(file['id'])
        if text is None:
            continue
        declarations, proof_spans = [], []
        for env, bstart, bend, estart, eend in _environment_spans(text):
            if env.rstrip('*') in names:
                labels = re.findall(r'\\label\s*\{([^{}]+)\}', text[bstart:eend])
                rows = _statement_anchor_rows(items, anchors, file['id'], label_map, labels, _line_of(text, bstart))
                declarations.append((bstart, eend, next(iter(rows)) if len(rows) == 1 else None))
            elif env == 'proof':
                proof_spans.append((bstart, eend))
        contexts = [(bstart, eend, owner, 'statement') for bstart, eend, owner in declarations]
        sections = _proof_section_spans(text, label_map, suffix_map)
        for pstart, pend in proof_spans:
            enclosing = [span for span in sections if span[2] is not None and span[0] <= pstart < span[1]]
            if enclosing:
                # A heading-attributed proof section outranks the whitespace
                # adjacency fallback for the proof environments it contains.
                owner = max(enclosing, key=lambda span: span[0])[2]
            else:
                prior = [(eend, owner) for bstart, eend, owner in declarations
                         if eend <= pstart and not text[eend:pstart].strip()]
                owner = max(prior, key=lambda entry: entry[0])[1] if prior else None
            contexts.append((pstart, pend, owner, 'proof'))
        contexts.extend(sections)
        proof_ranges = passage_lines.get((file['id'], 'proof'), [])
        statement_ranges = passage_lines.get((file['id'], 'statement'), [])
        for match in _REF_RE.finditer(text):
            containing = [span for span in contexts if span[0] <= match.start() < span[1]]
            if containing:
                citing, context = max(containing, key=lambda span: span[0])[2:]
            else:
                citing, context = None, 'narrative'
            line = _line_of(text, match.start())
            for label in (part.strip() for part in match.group(2).split(',')):
                if not label:
                    continue
                occurrence = {'file_id': file['id'], 'path': file['path'], 'line': line,
                              'command': '\\' + match.group(1), 'label': label, 'context': context}
                cited = label_map.get(label)
                if cited is None:
                    unmatched.setdefault(label, []).append(occurrence)
                elif citing is None:
                    recovered = None
                    if context != 'statement':
                        owners = {row for start, end, row in proof_ranges if start <= line <= end}
                        in_statement = any(start <= line <= end for start, end, _ in statement_ranges)
                        if len(owners) == 1 and not in_statement:
                            recovered = next(iter(owners))
                    if recovered is None:
                        unattributed.append(dict(occurrence, cited=cited))
                    elif recovered != cited:
                        pairs.setdefault((recovered, cited), []).append(dict(occurrence, attribution='proof_passage'))
                    # A row citing itself is not a dependency candidate, whether
                    # the owner came from source parsing or authored ranges.
                elif citing != cited:
                    pairs.setdefault((citing, cited), []).append(occurrence)
    uses_by_pair = {}
    for use in data['uses']:
        uses_by_pair.setdefault((use['to'], use['from']), []).append(use)
    pair_rows = []
    for (citing, cited), occurrences in pairs.items():
        occurrences.sort(key=lambda row: (row['path'], row['line'], row['label'], row['command']))
        recorded = [use['id'] for use in uses_by_pair.get((citing, cited), [])]
        pair_rows.append({'citing': citing, 'cited': cited, 'recorded_uses': recorded, 'occurrences': occurrences})
    pair_rows.sort(key=lambda row: (order[row['citing']], order[row['cited']]))
    missing = [row for row in pair_rows if not row['recorded_uses']]
    unsupported = []
    for use in data['uses']:
        if (use['to'], use['from']) in pairs:
            continue
        if 'infer' in (use['reason'] + ' ' + (use.get('issue') or '')).lower():
            continue
        unsupported.append({'id': use['id'], 'from': use['from'], 'to': use['to'],
                            'reason': use['reason'], 'cited_matchable': use['from'] in matchable})
    not_matchable = [{'id': row['id'], 'kind': row['kind'], 'label': row['label']}
                     for row in items if row['id'] not in matchable]
    unattributed.sort(key=lambda row: (row['path'], row['line'], row['label'], row['command']))
    unmatched_rows = [{'label': label, 'occurrences': sorted(occurrences, key=lambda row: (row['path'], row['line'], row['command']))}
                      for label, occurrences in sorted(unmatched.items())]
    attributed = sum(len(row['occurrences']) for row in pair_rows)
    recovered = sum(1 for row in pair_rows for occurrence in row['occurrences']
                    if occurrence.get('attribution') == 'proof_passage')
    note = 'A citation is evidence to review, not a recorded use; contribution, type, and regime require reading.'
    if recovered:
        note += (f' {recovered} occurrences were attributed through authored proof-passage ranges, only where '
                 'exactly one recorded proof passage contains the reference.')
    if unattributed:
        note += (f" {len(unattributed)} matched occurrences are unattributed, mostly main-text narrative "
                 'or unmarked proof sections; they were not used to form pairs.')
    if unsupported:
        note += (f' {len(unsupported)} recorded uses are not corroborated by this scan; that is missing '
                 'citation evidence, not a false dependency.')
    return {'snapshot_id': data['snapshot_id'], 'source_revision': data['source_revision']['id'],
            'note': note,
            'coverage': coverage,
            'attribution': {'attributed': attributed, 'unattributed': len(unattributed)},
            'counts': {'pairs': len(pair_rows),
                       'occurrences': sum(len(row['occurrences']) for row in pair_rows) + len(unattributed),
                       'missing_uses': len(missing), 'unsupported_uses': len(unsupported),
                       'unmatched_labels': len(unmatched_rows), 'not_mechanically_matchable': len(not_matchable)},
            'pairs': pair_rows,
            'missing_uses': [{key: row[key] for key in ('citing', 'cited', 'occurrences')} for row in missing],
            'unsupported_uses': unsupported,
            'unattributed_occurrences': unattributed,
            'unmatched_labels': unmatched_rows,
            'not_mechanically_matchable': not_matchable,
            'label_collisions': collisions}


def require_schema3(data):
    if not isinstance(data, dict):
        raise RecordError('Paper records: expected a JSON object.')
    if type(data.get('schema_version')) is not int or data['schema_version'] != 3:
        raise RecordError('Paper records: schema_version must be 3. Start a fresh schema-3 seed or supply a captured schema-3 export; older versions are not supported.')


def normalize(data, base_dir, extra_files=(), source_root=None):
    """Capture a native schema-3 seed or validate a captured schema-3 export.

    A seed has source locators and structured statements. An export has a
    source_revision and retained observations. Capture never creates reviews.
    """
    require_schema3(data)
    if 'source' in data and 'source_revision' in data:
        raise RecordError('Paper records: choose a compact seed with source or a captured export with source_revision, never both.')
    if 'source_revision' in data:
        captured = validate_records(data)
        if extra_files:
            return refresh_sources(captured, source_root or base_dir, extra_files=extra_files)
        return captured
    _fields(data, ('schema_version', 'title', 'scope', 'source', 'items', 'uses'), ('main_items',), 'Seed')
    root = data['source']
    _fields(root, ('title',), ('file',), 'Seed source')
    _text(root['title'], 'Seed source.title')
    if 'file' in root:
        _text(root['file'], 'Seed source.file')
    source_objects, item_passages = [root], []

    def located(source, context):
        _fields(source, (), ('file', 'label', 'page', 'start_line', 'end_line'), context)
        if 'file' in source:
            _text(source['file'], context + '.file')
        source_objects.append(source)
        return source

    for item in _rows(data['items'], 'Seed items'):
        _fields(item, ('id', 'kind', 'label', 'caption', 'statement'),
                ('source', 'passages', 'owner', 'aliases', 'issue'), 'Seed item')
        _id(item['id'], 'Seed item.id')
        passages = []
        if 'source' in item:
            passages.append(('statement', located(item['source'], 'Item source')))
        for passage in _rows(item.get('passages', []), 'Seed item passages'):
            _fields(passage, ('role', 'source'), (), 'Seed passage')
            passages.append((passage['role'], located(passage['source'], 'Passage source')))
        if not passages:
            raise RecordError(f"Item {item['id']}: provide source or at least one passage with a source locator.")
        item_passages.append(passages)
    reserved_ids = set()
    for use in _rows(data['uses'], 'Seed uses'):
        _fields(use, ('from', 'to', 'reason'), ('id', 'type', 'regime', 'group', 'issue', 'source'), 'Seed use')
        for endpoint in ('from', 'to'):
            _id(use[endpoint], 'Seed use.' + endpoint)
        if 'id' in use:
            _id(use['id'], 'Seed use.id')
            if use['id'] in reserved_ids:
                raise RecordError(f"Use {use['id']}: duplicate identity. Distinct uses need distinct IDs.")
            reserved_ids.add(use['id'])
        if 'source' in use:
            located(use['source'], 'Use source')
    capture_base = Path(source_root or base_dir).resolve()
    # Seed references are relative to the seed. Captured paths are relative to
    # the manuscript root, which can differ from the output/work directory.
    paths = [(Path(base_dir) / s.get('file', root.get('file'))).resolve()
             for s in source_objects if s.get('file', root.get('file'))] + list(extra_files)
    files, unresolved = _capture(paths, capture_base)
    revision = {'id': _source_digest(files), 'title': root['title'], 'created_at': _now(), 'files': files}
    by_path = {(capture_base / f['path']).resolve(): f for f in files}
    anchors, anchor_ids = [], set()

    def anchor(source, identity):
        stem, suffix = identity, 1
        while identity in anchor_ids:
            suffix += 1
            identity = f'{stem}-{suffix}'
        anchor_ids.add(identity)
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
    for item, passages in zip(data['items'], item_passages):
        row = {k: copy.deepcopy(v) for k, v in item.items() if k not in {'source', 'passages'}}
        row['passages'] = [{'role': role, 'anchor_id': anchor(source, 'anchor-item-' + item['id'] + (f'-{index}' if index else ''))}
                           for index, (role, source) in enumerate(passages)]
        items.append(row)
    for use in data['uses']:
        row = {k: copy.deepcopy(v) for k, v in use.items() if k != 'source'}
        if 'id' not in row:
            stem = 'use-' + _sha((row['from'] + '\0' + row['to']).encode())[:16]
            identity, suffix = stem, 1
            while identity in reserved_ids:
                suffix += 1
                identity = f'{stem}-{suffix}'
            row['id'] = identity
            reserved_ids.add(identity)
        row.setdefault('type', 'dependency')
        row['evidence_refs'] = [anchor(use['source'], 'anchor-' + row['id'])] if 'source' in use else []
        uses.append(row)
    result = {'schema_version': 3, 'title': data['title'], 'scope': data['scope'],
              'source_revision': revision, 'anchors': anchors, 'items': items, 'uses': uses,
              'observations': []}
    if 'main_items' in data:
        result['main_items'] = copy.deepcopy(data['main_items'])
    result = validate_records(result)
    result.pop('snapshot_id')
    result['inventory'] = _inventory(revision, items, anchors, unresolved)
    return validate_records(result)


def validate_records(data):
    """Validate content, captured evidence and references; allow cyclic mappings."""
    require_schema3(data)
    _fields(data, ('schema_version', 'title', 'scope', 'source_revision', 'anchors', 'items', 'uses', 'observations'),
            ('snapshot_id', 'inventory', 'main_items'), 'Paper records')
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
        _fields(row, ('id', 'kind', 'label', 'caption', 'statement', 'passages'),
                ('aliases', 'issue', 'owner'), 'Item')
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
        if row.get('issue') is not None:
            _text(row['issue'], row['label'] + ' issue')
        item_map[row['id']] = row
    if not item_map:
        raise RecordError('Paper records: retain at least one major item.')
    for row in result['items']:
        owner = row.get('owner')
        if row['kind'] in INTERMEDIATE_KINDS:
            if not isinstance(owner, str) or item_map.get(owner, {}).get('kind') not in MAJOR_KINDS:
                raise RecordError(f"{row['label']}: an intermediate {row['kind']} needs owner naming an existing major item. Re-own or remove it before removing its owner.")
        elif owner is not None:
            raise RecordError(f"{row['label']}: a major item does not take an owner; remove the field.")
    use_map = {}
    group_kinds = {}
    for row in _rows(result['uses'], 'Uses'):
        _fields(row, ('id', 'from', 'to', 'type', 'reason', 'evidence_refs'),
                ('regime', 'issue', 'group'), 'Use')
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
        if 'regime' in row:
            _text(row['regime'], 'Use.regime')
        if row.get('issue') is not None:
            _text(row['issue'], 'Use.issue')
        group = row.get('group')
        if group is not None:
            _fields(group, ('id', 'kind'), (), f"Use {row['id']} group")
            _text(group['id'], f"Use {row['id']} group.id")
            if group['kind'] not in ('joint', 'cases'):
                raise RecordError(f"Use {row['id']}: group kind must be joint or cases.")
            prior = group_kinds.setdefault(group['id'], group['kind'])
            if prior != group['kind']:
                raise RecordError(f"Use {row['id']}: group {group['id']!r} is already recorded as {prior!r}; one group id keeps one consistent kind.")
        refs = _rows(row['evidence_refs'], 'Use evidence_refs')
        if any(not isinstance(ref, str) or ref not in anchor_map for ref in refs) or len(set(refs)) != len(refs):
            raise RecordError(f"Use {row['id']}: evidence references must be distinct existing anchor IDs.")
        use_map[row['id']] = row
    if 'main_items' in result:
        main = _rows(result['main_items'], 'main_items')
        if not main or any(not isinstance(k, str) or k not in item_map for k in main) or len(set(main)) != len(main):
            raise RecordError('main_items: choose distinct existing item IDs, or omit the field.')
        if any(item_map[k]['kind'] not in MAJOR_KINDS for k in main):
            raise RecordError('main_items: intermediate rows stay inside their owner; choose major items only.')
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
            if decl['file_id'] not in file_map or decl['kind'] not in MAJOR_KINDS:
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


def _digest_row(row):
    # Explicit null optional fields have the same meaning as absent fields.
    return {key: value for key, value in row.items() if not (value is None and key in ('owner', 'group', 'issue'))}


def review_context(data, item_id):
    """Bounded review context of one row, shared by digests, packets, revisions.

    S = the row plus its directly owned intermediate rows. The context is the
    rows in S, every use whose target is in S, those uses' prerequisite rows,
    and the passages/evidence anchors they involve. Row order is target, owned
    rows in record order, then prerequisites in use order (duplicates
    retained), so a row without intermediate context keeps its previous
    selection exactly. Per-use comparison targets keep their own policy.
    """
    items = {row['id']: row for row in data['items']}
    if item_id not in items:
        raise RecordError(f"Review context target items/{item_id} is absent.")
    owned = [row for row in data['items'] if row.get('owner') == item_id]
    scope = {item_id} | {row['id'] for row in owned}
    uses = [use for use in data['uses'] if use['to'] in scope]
    context_items = [items[item_id]] + owned + [items[use['from']] for use in uses]
    anchor_ids = {passage['anchor_id'] for row in context_items for passage in row['passages']}
    anchor_ids.update(ref for use in uses for ref in use['evidence_refs'])
    return {'target': items[item_id], 'owned': owned, 'scope': scope,
            'uses': uses, 'items': context_items, 'anchor_ids': anchor_ids}


def target_digest(data, collection, identity):
    _text(collection, 'Comparison collection')
    if collection not in {'items', 'uses'}:
        raise RecordError('Comparison target collection must be items or uses.')
    records = {row['id']: row for row in data[collection]}
    if identity not in records:
        raise RecordError(f"Comparison target {collection}/{identity} is absent.")
    target = records[identity]
    context = {'target': _digest_row(target), 'source_revision': data['source_revision']['id']}
    if collection == 'items':
        selected = review_context(data, identity)
        uses, relevant_items, anchors = selected['uses'], selected['items'], selected['anchor_ids']
    else:
        items = {row['id']: row for row in data['items']}
        uses, relevant_items = [target], [items[target['from']], items[target['to']]]
        anchors = {p['anchor_id'] for i in relevant_items for p in i['passages']}
        anchors.update(a for u in uses for a in u['evidence_refs'])
    context.update(uses=[_digest_row(u) for u in uses], items=[_digest_row(i) for i in relevant_items],
                   anchors=[a for a in data['anchors'] if a['id'] in anchors])
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


def fidelity_by_row(data):
    """Per-row review fidelity: newest applicable observation, else stale or unreviewed."""
    latest = {(o['target']['collection'], o['target']['id']): o for o in applicable_observations(data)}
    fidelity = {}
    for collection in ('items', 'uses'):
        for row in data[collection]:
            observation = latest.get((collection, row['id']))
            fidelity[(collection, row['id'])] = 'unreviewed' if observation is None else (
                'stale' if observation['input_snapshot'] != target_digest(data, collection, row['id']) else observation['result'])
    return fidelity


def comparison_status(data, fidelity=None):
    fidelity = fidelity if fidelity is not None else fidelity_by_row(data)
    counts = {'matched': 0, 'needs_attention': 0, 'unreviewed': 0, 'stale': 0,
              'total': len(data['items']) + len(data['uses'])}
    for status in fidelity.values():
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


def record_report(data, base_dir):
    """Structural/source diagnostics for validated records, without math rendering."""
    report = {'warnings': []}
    major_ids = {row['id'] for row in data['items'] if row['kind'] in MAJOR_KINDS}
    # Layout constraints do not invalidate the paper dataset. The cycle check
    # consumes graph edges only; detail edges never affect the layout (R4b).
    incoming = {key: 0 for key in major_ids}
    outgoing = {key: [] for key in major_ids}
    for use in data['uses']:
        if use['from'] not in major_ids or use['to'] not in major_ids:
            continue
        incoming[use['to']] += 1
        outgoing[use['from']].append(use['to'])
    ready = [key for key in incoming if incoming[key] == 0]
    for key in ready:
        for target in outgoing[key]:
            incoming[target] -= 1
            if not incoming[target]:
                ready.append(target)
    report['graph_mode'] = 'dag' if len(ready) == len(incoming) else 'index'
    if report['graph_mode'] == 'index':
        report['warnings'].append('The combined dependency map contains a cycle. All items and uses are retained in the index; this layout limitation does not establish a circular proof. Compare the directions and alternative regimes with the manuscript.')
    freshness = source_status(data, base_dir)
    if freshness != 'current':
        report['warnings'].append({'historical_changed': 'The live manuscript differs from this captured source version. This overview displays historical records; refresh and compare them before calling it current.',
                                     'historical_unavailable': 'Some live source files are unavailable. This overview uses its captured historical source version.',
                                     'unregistered': 'No source files are registered. Entered locators and completeness need source comparison.'}[freshness])
    status = comparison_status(data)
    if status['status'] != 'complete':
        report['warnings'].append(f"Source comparison is incomplete: {status['unreviewed']} records unreviewed, {status['stale']} comparisons stale, {status['needs_attention']} needing attention. These are overview comparisons, not proof verdicts.")
    unlocated = sorted(row['id'] for row in data['uses'] if not row['evidence_refs'])
    report['uses_without_evidence'] = unlocated
    if unlocated:
        report['warnings'].append(f"{len(unlocated)} uses have no located evidence passages. Locate the supporting passage in the captured source or disclose the gap; validation output lists their IDs.")
    unverified = sum(a['verification']['status'] != 'checked' for a in data['anchors'])
    if unverified:
        methods = [set(part.strip() for part in a['verification']['method'].split(','))
                   for a in data['anchors']]
        checks = [(sum(method in found for found in methods), label)
                  for method, label in (('line_range', 'text line ranges'),
                                        ('pdf_page_bounds', 'PDF page bounds'),
                                        ('tex_label', 'TeX labels'))]
        checked = ', '.join(f'{count} {label}' for count, label in checks if count)
        summary = 'Source locator checks: ' + checked if checked else 'No mechanical source locator checks are recorded'
        labels = sum('label' in a['locator'] and 'tex_label' not in found
                     for a, found in zip(data['anchors'], methods))
        pages = sum('page' in a['locator'] and 'pdf_page_bounds' not in found
                    for a, found in zip(data['anchors'], methods))
        pending = []
        if labels:
            pending.append(f'{labels} entered/printed labels not mechanically matched')
        if pages:
            pending.append(f'{pages} PDF page locations not checked')
        details = f" ({'; '.join(pending)})" if pending else ''
        report['warnings'].append(f"{summary}; {unverified} anchors still have locator details requiring source comparison{details}. Location checks do not establish that a passage states the recorded claim.")
    inventory = data.get('inventory', {})
    missing = sum(not d['item_ids'] for d in inventory.get('declarations', []))
    if missing:
        report['warnings'].append(f"{missing} detected declarations are not matched to overview items. Check the selected scope and candidate inventory.")
    report['warnings'].extend(inventory.get('unresolved', []))
    report['warnings'].extend('Scope exclusion: ' + note for note in inventory.get('excluded', []))
    report['build_context'] = {'input_snapshot': data['snapshot_id'], 'source_revision': data['source_revision']['id'],
                                 'source_status': freshness, 'source_comparison': status,
                                 'citation_candidates': citation_summary(data),
                                 'mathematical_assessment': 'not_performed'}
    report['inventory'] = {k: v for k, v in inventory.items() if k != 'declarations'}
    return report


def prepare_records(data, base_dir):
    """The single database-to-display projection.

    Major rows become graph items; intermediate rows become owner-tagged,
    id-sorted details; uses split into graph edges (both endpoints major) and
    detail uses (at least one intermediate endpoint). Detail rows and edges
    never enter the layout, the cycle check, or the terminal selection.
    """
    data = validate_records(data)
    files = {f['id']: f for f in data['source_revision']['files']}
    anchors = {a['id']: a for a in data['anchors']}
    prepared = {'schema_version': 3, 'title': data['title'], 'scope': data['scope'],
                'source': {'title': data['source_revision']['title']},
                'items': [], 'details': [], 'uses': [], 'detail_uses': [], 'warnings': []}
    fidelity = fidelity_by_row(data)

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
                'source_file': files[anchor['file_id']]['path'] if anchor.get('file_id') else None,
                'source_media_type': files[anchor['file_id']]['media_type'] if anchor.get('file_id') else None}

    def prepare_item(item):
        row = copy.deepcopy(item)
        row['statement_form'] = row['statement']['form']
        row['statement'] = row['statement']['text']
        row['statement_diagnostics'] = []
        row['statement_html'] = render_text(row['statement'], diagnostics=row['statement_diagnostics'])
        row['source_passages'] = [passage(p['anchor_id'], p['role']) for p in row['passages']]
        primary = next((p for p in row['source_passages'] if p['role'] == 'statement'), row['source_passages'][0])
        row.update({k: primary[k] for k in ('source_display', 'source_excerpt')})
        row['fidelity'] = fidelity[('items', row['id'])]
        return row

    def prepare_use(use):
        row = copy.deepcopy(use)
        row['reason_diagnostics'] = []
        row['reason_html'] = render_text(row['reason'], diagnostics=row['reason_diagnostics'])
        row['source_passages'] = [passage(a, 'evidence') for a in row['evidence_refs']]
        if row['source_passages']:
            row.update({k: row['source_passages'][0][k] for k in ('source_display', 'source_excerpt')})
        row['fidelity'] = fidelity[('uses', row['id'])]
        return row

    prepared['items'] = [prepare_item(item) for item in data['items'] if item['kind'] in MAJOR_KINDS]
    prepared['details'] = sorted((prepare_item(item) for item in data['items'] if item['kind'] in INTERMEDIATE_KINDS),
                                 key=lambda row: row['id'])
    major_ids = {row['id'] for row in prepared['items']}
    for use in data['uses']:
        row = prepare_use(use)
        target = 'uses' if use['from'] in major_ids and use['to'] in major_ids else 'detail_uses'
        prepared[target].append(row)
    if 'main_items' in data:
        prepared['main_items'] = data['main_items'][:]
    prepared.update(record_report(data, base_dir))
    diagnostics = []
    for row in prepared['items'] + prepared['details'] + prepared['uses'] + prepared['detail_uses']:
        for field in ('statement', 'reason'):
            for entry in row.get(field + '_diagnostics', []):
                diagnostics.append({'collection': 'items' if 'kind' in row else 'uses',
                                    'id': row['id'], 'field': field, **entry})
            row.pop(field + '_diagnostics', None)
    prepared['math_diagnostics'] = diagnostics
    if diagnostics:
        located = '; '.join(f"{entry['id']} ({entry['field']}): {entry['reason']}" for entry in diagnostics[:5])
        prepared['warnings'].append(
            f"{len(diagnostics)} LaTeX expressions could not be typeset; the original expressions remain visibly "
            f"labeled. {located}{'; …' if len(diagnostics) > 5 else ''} The render receipt's math_diagnostics "
            "lists every failure with its record id, field, excerpt, and reason.")
    return prepared
