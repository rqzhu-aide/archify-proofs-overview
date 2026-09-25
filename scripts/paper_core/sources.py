"""Source capture, anchoring, and source review (implementation-handoff 5; record-contract 2-3).

Capture reads the files listed relative to the registered root plus the literal
local TeX inputs they name; it never crawls directories. Anchors are resolved
in code: the command extracts the excerpt bytes, hashes them, and records the
method (`exact_lines`, `label_match`, `reviewed_page`, `exact_relocation`).
Source reviews and issues travel in the generic edit envelope restricted to
their collections. Adapted from proof-graphify ``paper_records.py``
(``_capture``, ``_binding``, ``_inventory``) with the same discovery rules.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

from .acceptance import accept
from .canonical import digest, sha256_bytes
from .contract import ANCHOR_REQUEST, BATCH, validate_shape
from .errors import InvalidRequest, SourceUnavailable
from .ids import new_id
from .storage import Database, paper_record

MEDIA_BY_SUFFIX = {".tex": "tex", ".ltx": "tex", ".sty": "tex", ".cls": "tex", ".bib": "bib", ".bbl": "bib",
                   ".pdf": "pdf", ".txt": "text", ".md": "text", ".rst": "text"}
TEX_SUFFIXES = {".tex", ".ltx", ".sty", ".cls"}
KIND_WORDS = (("theorem", "theorem"), ("lemma", "lemma"), ("proposition", "proposition"),
              ("corollary", "corollary"), ("assumption", "assumption"), ("condition", "assumption"),
              ("definition", "definition"))
DEFAULT_ENVIRONMENTS = ("theorem", "lemma", "proposition", "corollary", "definition", "assumption",
                        "condition", "remark", "example", "claim", "conjecture")
SHORTHANDS = {"thm": "theorem", "theo": "theorem", "lem": "lemma", "prop": "proposition", "cor": "corollary",
              "coro": "corollary", "ass": "assumption", "assum": "assumption", "asm": "assumption",
              "defn": "definition", "defi": "definition", "dfn": "definition"}
INPUT_RE = re.compile(r"\\(?:input|include|subfile)\s*\{([^{}]+)\}")
INPUT_FORMS_RE = re.compile(r"\\(?:input|include|subfile)\b(?!\s*\{)|\\(?:import|subimport|inputfrom)\b")
PACKAGE_RE = re.compile(r"\\(usepackage|RequirePackage|documentclass)\s*(?:\[[^]]*\])?\s*\{([^{}]+)\}")
NEWTHEOREM_RE = re.compile(r"\\newtheorem\*?\s*\{([^{}]+)\}\s*(?:\[[^\]]*\])?\s*\{([^{}]*)\}")
DECLARETHEOREM_RE = re.compile(r"\\declaretheorem\s*(?:\[[^\]]*\])?\s*\{([^{}]+)\}")
ENV_TOKEN_RE = re.compile(r"\\(begin|end)\s*\{([A-Za-z*]+)\}")
CITE_RE = re.compile(r"\\[cC]ite[A-Za-z*]*\s*(?:\[[^\]]*\]\s*){0,2}\{([^{}]+)\}")
BIBITEM_RE = re.compile(r"\\bibitem\s*(?:\[[^\]]*\])?\s*\{([^{}]+)\}")
BIBENTRY_RE = re.compile(r"@\w+\s*\{\s*([^,\s]+)\s*,")

try:  # pypdf is optional; page anchors record the limitation when it is missing
    from pypdf import PdfReader
except ImportError:  # pragma: no cover - depends on the installation
    PdfReader = None


def media_type(path: str) -> str:
    return MEDIA_BY_SUFFIX.get(Path(path).suffix.lower(), "other")


def uncomment(text: str) -> str:
    """Drop TeX comments while preserving every newline, so offsets still map to lines."""
    return re.sub(r"(?<!\\)((?:\\\\)*)%[^\n]*", r"\1", text)


def _decode(raw: bytes):
    try:
        return raw.decode("utf-8-sig")
    except UnicodeError:
        return None


def _kind_for(environment: str, heading: str):
    text = f"{environment} {heading}".lower()
    for word, kind in KIND_WORDS:
        if word in text:
            return kind
    short = SHORTHANDS.get(environment.lower())
    return short


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


# -- capture ----------------------------------------------------------------

def discover(root, files: list) -> tuple[list, list]:
    """Resolve LIST.json entries and the literal local TeX inputs they name.

    Returns captured file descriptions (path, media_type, bytes, sha256,
    capture_method, limitation) and unresolved notes. Relative entries resolve
    against the registered root and must stay inside it. Absolute entries are
    explicit registrations; outside the root they keep their absolute path and
    a limitation note. Discovered inputs are captured only inside the root.
    """
    root = Path(root).resolve()
    if not root.is_dir():
        raise SourceUnavailable(f"registered source root is not a directory: {root}")
    if not isinstance(files, list) or not files or not all(isinstance(f, str) and f.strip() for f in files):
        raise InvalidRequest("LIST.json must be a nonempty array of path strings")
    seeds = []
    for entry in files:
        given = Path(entry)
        if given.is_absolute():
            resolved = given.resolve()
            inside = resolved == root or resolved.is_relative_to(root)
            seeds.append((resolved, "listed", None if inside else "registered by absolute path outside the source root"))
        else:
            resolved = (root / given).resolve()
            if not resolved.is_relative_to(root):
                raise InvalidRequest(f"path {entry!r} escapes the registered source root")
            seeds.append((resolved, "listed", None))
    roots = []
    for path, _, _ in seeds:
        if path.suffix.lower() == ".tex" and path.is_file():
            try:
                if re.search(r"\\documentclass\b", uncomment(path.read_text(encoding="utf-8-sig"))):
                    roots.append(path.parent)
            except (OSError, UnicodeError):
                pass
    roots = list(dict.fromkeys(roots)) or [seeds[0][0].parent]

    def compile_root(path):
        choices = [r for r in roots if path.is_relative_to(r)]
        return max(choices, key=lambda r: len(r.parts)) if choices else path.parent

    queue = [(path, compile_root(path), method, limitation) for path, method, limitation in seeds]
    captured, notes = {}, []
    while queue:
        path, croot, method, limitation = queue.pop(0)
        if path in captured:
            continue
        if not path.is_file():
            raise SourceUnavailable(f"cannot capture {path.as_posix()}: not a readable file; supply the source or "
                                    "correct its path")
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise SourceUnavailable(f"cannot capture {path.as_posix()}: {exc}") from exc
        display = path.relative_to(root).as_posix() if (path == root or path.is_relative_to(root)) else path.as_posix()
        captured[path] = {"path": display, "media_type": media_type(display), "bytes": raw,
                          "sha256": sha256_bytes(raw), "capture_method": method, "limitation": limitation}
        if path.suffix.lower() not in TEX_SUFFIXES:
            continue
        text = _decode(raw)
        if text is None:
            notes.append(f"{display}: source discovery requires UTF-8; register relevant inputs explicitly")
            continue
        text = uncomment(text)
        for match in INPUT_RE.finditer(text):
            name = match.group(1).strip()
            if "\\" in name or "#" in name:
                notes.append(f"{display}: dynamic input {name!r}; register the resolved source explicitly")
                continue
            candidates = [croot / name, path.parent / name]
            candidates = [c if c.suffix else c.with_suffix(".tex") for c in candidates]
            found = next((c for c in candidates if c.is_file()), None)
            if found is None:
                notes.append(f"{display}: unresolved input {name!r}; register or explain the missing source")
            elif not found.resolve().is_relative_to(root):
                notes.append(f"{display}: input {name!r} lies outside the source root; register it explicitly")
            else:
                queue.append((found.resolve(), croot, "tex_input", None))
        if INPUT_FORMS_RE.search(text):
            notes.append(f"{display}: an input form needs manual source registration")
        for match in PACKAGE_RE.finditer(text):
            suffix = ".cls" if match.group(1) == "documentclass" else ".sty"
            for name in match.group(2).split(","):
                candidates = [croot / (name.strip() + suffix), path.parent / (name.strip() + suffix)]
                found = next((c for c in candidates if c.is_file()), None)
                if found is not None and found.resolve().is_relative_to(root):
                    queue.append((found.resolve(), croot, "tex_input", None))
    return list(captured.values()), list(dict.fromkeys(notes))


def declarations(text: str) -> list:
    """Theorem-like environments declared and used in one TeX source (adapted from archify _inventory)."""
    clean = uncomment(text)
    headings = {env: env.capitalize() for env in DEFAULT_ENVIRONMENTS}
    for match in NEWTHEOREM_RE.finditer(clean):
        headings[match.group(1).strip()] = match.group(2).strip() or match.group(1).strip()
    for match in DECLARETHEOREM_RE.finditer(clean):
        headings.setdefault(match.group(1).strip(), match.group(1).strip().capitalize())
    tokens = [(m.start(), m.end(), m.group(1), m.group(2)) for m in ENV_TOKEN_RE.finditer(clean)]
    stack, spans = [], []
    for start, end, kind, env in tokens:
        if kind == "begin":
            stack.append((env, start, end))
            continue
        for depth in range(len(stack) - 1, -1, -1):
            if stack[depth][0] == env:
                _, bstart, bend = stack[depth]
                del stack[depth:]
                spans.append((env, bstart, bend, start, end))
                break
    spans.sort(key=lambda s: s[1])
    result = []
    proofs = [(s[1], s[3]) for s in spans if s[0] == "proof"]
    for env, bstart, bend, estart, eend in spans:
        if env not in headings:
            continue
        body = clean[bend:estart]
        title_match = re.match(r"\s*\[([^\]]*)\]", body)
        label_match = re.search(r"\\label\s*\{\s*([^{}\s]+)\s*\}", body)
        proof = None
        for pstart, pend in proofs:
            if pstart >= eend and not clean[eend:pstart].strip():
                proof = {"start_line": _line_of(clean, pstart), "end_line": _line_of(clean, pend)}
                break
        result.append({"environment": env, "kind": _kind_for(env, headings[env]), "heading": headings[env],
                       "title": title_match.group(1).strip() if title_match else None,
                       "label": label_match.group(1) if label_match else None,
                       "start_line": _line_of(clean, bstart), "end_line": _line_of(clean, estart),
                       "proof": proof})
    return result


def citations(text: str, media: str) -> dict:
    clean = uncomment(text) if media == "tex" else text
    keys = set()
    for match in CITE_RE.finditer(clean):
        keys.update(k.strip() for k in match.group(1).split(",") if k.strip())
    bibitems = sorted({m.group(1).strip() for m in BIBITEM_RE.finditer(clean)})
    entries = sorted({m.group(1).strip() for m in BIBENTRY_RE.finditer(clean)}) if media == "bib" else []
    return {"cite_keys": sorted(keys), "bibitems": bibitems, "bib_entries": entries}


def capture_sources(db: Database, *, files: list, request_id=None) -> dict:
    """Capture listed files (plus literal TeX inputs); new source versions only when content changed."""
    if not db.write:
        raise InvalidRequest("source capture needs a writable database")
    paper = paper_record(db)
    captured, notes = discover(paper.body["source_root"], files)
    live = {s.body["path"]: s for s in db.heads("sources")}
    edits, blobs, listing = [], [], []
    for entry in captured:
        prior = live.get(entry["path"])
        body = {"paper_id": paper.id, "path": entry["path"], "media_type": entry["media_type"],
                "blob_sha256": entry["sha256"], "capture_method": entry["capture_method"],
                "limitation": entry["limitation"]}
        if prior is not None and prior.body["blob_sha256"] == entry["sha256"]:
            listing.append({"id": prior.id, "version": prior.version, "path": entry["path"],
                            "media_type": entry["media_type"], "blob_sha256": entry["sha256"], "changed": False})
            continue
        blobs.append(entry["bytes"])
        if prior is None:
            source_id = new_id("sources")
            edits.append({"op": "create", "collection": "sources", "id": source_id, "expected_version": None,
                          "body": body})
            version = 1
        else:
            source_id = prior.id
            edits.append({"op": "replace", "collection": "sources", "id": source_id,
                          "expected_version": prior.version, "body": body})
            version = prior.version + 1
        listing.append({"id": source_id, "version": version, "path": entry["path"], "media_type": entry["media_type"],
                        "blob_sha256": entry["sha256"], "changed": True})
    receipt = None
    if edits:
        request_id = request_id or new_id("request")
        request_digest = digest({"command": "source_capture",
                                 "files": [{"path": e["path"], "sha256": e["sha256"]} for e in captured]})
        receipt = accept(db, request_id=request_id, request_digest=request_digest, packet_id=None, edits=edits,
                         command="source_capture", blobs=blobs, warnings=notes)
    by_path = {e["path"]: e for e in listing}
    declared, cited, limitations = [], [], list(notes)
    for entry in captured:
        source = by_path[entry["path"]]
        if entry["limitation"]:
            limitations.append(f"{entry['path']}: {entry['limitation']}")
        if entry["media_type"] in ("tex", "bib", "text"):
            text = _decode(entry["bytes"])
            if text is None:
                limitations.append(f"{entry['path']}: not UTF-8 text; declarations and citations were not scanned")
                continue
            if entry["media_type"] == "tex":
                for declaration in declarations(text):
                    declared.append({"source_id": source["id"], "path": entry["path"], **declaration})
            cites = citations(text, entry["media_type"])
            if cites["cite_keys"] or cites["bibitems"] or cites["bib_entries"]:
                cited.append({"source_id": source["id"], "path": entry["path"], **cites})
        elif entry["media_type"] == "pdf":
            limitations.append(f"{entry['path']}: PDF sources anchor by reviewed page; text extraction is approximate")
    return {"receipt": receipt, "changed": bool(edits), "sources": listing, "declarations": declared,
            "citations": cited, "limitations": list(dict.fromkeys(limitations))}


# -- anchors ------------------------------------------------------------------

def _source_text(db: Database, source) -> str:
    raw = db.get_blob(source.body["blob_sha256"])
    if raw is None:
        raise SourceUnavailable(f"source {source.id} content blob {source.body['blob_sha256']} is missing")
    text = _decode(raw)
    if text is None:
        raise InvalidRequest(f"source {source.id} ({source.body['path']}) is not UTF-8 text; line anchors need text")
    return text


def _enclosing_environment(clean: str, offset: int):
    """Innermost non-document environment containing ``offset`` as (start, end) offsets, or None."""
    stack, best = [], None
    for match in ENV_TOKEN_RE.finditer(clean):
        if match.group(2) == "document":
            continue
        if match.group(1) == "begin":
            stack.append((match.group(2), match.start()))
            continue
        for depth in range(len(stack) - 1, -1, -1):
            if stack[depth][0] == match.group(2):
                env, start = stack[depth]
                del stack[depth:]
                if start <= offset < match.end() and (best is None or start >= best[0]):
                    best = (start, match.end())
                break
    return best


def _resolve_label(text: str, label: str):
    clean = uncomment(text)
    matches = list(re.finditer(r"\\label\s*\{\s*" + re.escape(label) + r"\s*\}", clean))
    if not matches:
        raise InvalidRequest(f"label {label!r} does not occur in the source", code="LABEL_NOT_FOUND")
    if len(matches) > 1:
        raise InvalidRequest(f"label {label!r} occurs {len(matches)} times; record an ambiguous_label source issue "
                             "or anchor by exact lines", code="AMBIGUOUS_LABEL",
                             records=[_line_of(clean, m.start()) for m in matches])
    span = _enclosing_environment(clean, matches[0].start())
    if span is None:
        line = _line_of(clean, matches[0].start())
        return line, line, "label is not inside an environment; anchored to the label line only"
    return _line_of(clean, span[0]), _line_of(clean, span[1] - 1), None


def _extract_lines(text: str, start: int, end: int) -> str:
    lines = text.splitlines()
    if end > len(lines):
        raise InvalidRequest(f"lines {start}-{end} exceed the source ({len(lines)} lines); re-anchor this passage",
                             code="LINES_OUT_OF_RANGE")
    return "\n".join(lines[start - 1:end])


def _extract_page(raw: bytes, page: int):
    from .pdf_text import sanitize_pdf_excerpt

    if PdfReader is None:
        return "", "pypdf is not installed; page text was not extracted"
    try:
        reader = PdfReader(io.BytesIO(raw))
        count = len(reader.pages)
    except Exception as exc:  # pragma: no cover - depends on the PDF
        raise InvalidRequest(f"cannot read the PDF source: {exc}") from exc
    if page > count:
        raise InvalidRequest(f"page {page} exceeds the PDF ({count} pages)", code="PAGE_OUT_OF_RANGE")
    try:
        excerpt = reader.pages[page - 1].extract_text() or ""
    except Exception:  # pragma: no cover - depends on the PDF
        return "", "PDF page text could not be extracted; inspect the original page."
    excerpt, limitation = sanitize_pdf_excerpt(excerpt)
    return excerpt, limitation or "PDF text extraction is approximate; compare important formulas with the original page."


def resolve_anchor(db: Database, source, locator: dict, prior=None) -> dict:
    """Extract one anchor's excerpt and method from the live source version."""
    locator = dict(locator)
    media = source.body["media_type"]
    if locator["page"] is not None:
        if media != "pdf":
            raise InvalidRequest("page locators require a PDF source")
        if locator["start_line"] is not None:
            raise InvalidRequest("a page locator cannot combine with line numbers")
        raw = db.get_blob(source.body["blob_sha256"])
        if raw is None:
            raise SourceUnavailable(f"source {source.id} content blob is missing")
        excerpt, limitation = _extract_page(raw, locator["page"])
        limitation = f"{source.body['path']}, physical PDF page {locator['page']}: {limitation}"
        method = "reviewed_page"
    else:
        if media == "pdf":
            raise InvalidRequest("PDF sources anchor by page")
        text = _source_text(db, source)
        limitation = None
        if locator["start_line"] is not None:
            excerpt = _extract_lines(text, locator["start_line"], locator["end_line"])
            if locator["label"] is not None and not re.search(
                    r"\\label\s*\{\s*" + re.escape(locator["label"]) + r"\s*\}", uncomment(excerpt)):
                raise InvalidRequest(f"label {locator['label']!r} does not occur within lines "
                                     f"{locator['start_line']}-{locator['end_line']}", code="LABEL_NOT_IN_RANGE")
            method = "exact_lines"
        else:
            start, end, limitation = _resolve_label(text, locator["label"])
            locator["start_line"], locator["end_line"] = start, end
            excerpt = _extract_lines(text, start, end)
            method = "label_match"
    if prior is not None and prior.body["excerpt"] == excerpt and (
            prior.body["source_version"] != source.version or prior.body["locator"] != locator):
        method = "exact_relocation"
    return {"source_id": source.id, "source_version": source.version, "locator": locator, "excerpt": excerpt,
            "excerpt_sha256": sha256_bytes(excerpt.encode("utf-8")), "method": method, "limitation": limitation}


def anchor_sources(db: Database, *, request: dict) -> dict:
    """Create or rebind anchors from an anchor request (handoff 5 file conventions)."""
    errors = validate_shape(ANCHOR_REQUEST, request)
    if errors:
        raise InvalidRequest("invalid anchor request", records=errors)
    if not request["anchors"]:
        raise InvalidRequest("anchor request lists no anchors")
    edits, listing, unchanged = [], [], []
    for index, entry in enumerate(request["anchors"]):
        source = db.head("sources", entry["source_id"])
        if source is None or source.retired:
            raise InvalidRequest(f"anchors/{index}: source {entry['source_id']} is not a live record")
        prior = db.head("anchors", entry["id"])
        if entry["expected_version"] is None:
            if prior is not None:
                raise InvalidRequest(f"anchors/{index}: anchor {entry['id']} exists at version {prior.version}; "
                                     "pass expected_version to rebind it")
            body = resolve_anchor(db, source, entry["locator"])
            edits.append({"op": "create", "collection": "anchors", "id": entry["id"], "expected_version": None,
                          "body": body})
            version = 1
        else:
            if prior is None:
                raise InvalidRequest(f"anchors/{index}: anchor {entry['id']} does not exist")
            body = resolve_anchor(db, source, entry["locator"], prior if not prior.retired else None)
            if prior.body == body:
                unchanged.append({"id": entry["id"], "version": prior.version})
                continue
            edits.append({"op": "replace", "collection": "anchors", "id": entry["id"],
                          "expected_version": entry["expected_version"], "body": body})
            version = entry["expected_version"] + 1
        listing.append({"id": entry["id"], "version": version, "method": body["method"], "source_id": source.id,
                        "source_version": source.version, "locator": body["locator"],
                        "excerpt_sha256": body["excerpt_sha256"], "limitation": body["limitation"]})
    receipt = None
    if edits:
        receipt = accept(db, request_id=request["request_id"], request_digest=digest(request),
                         packet_id=request["packet_id"], edits=edits, command="source_anchor")
    return {"receipt": receipt, "anchors": listing, "unchanged": unchanged}


# -- reviews ------------------------------------------------------------------

def review_sources(db: Database, *, batch: dict) -> dict:
    """Record source reviews and source issues; pinned refs must name live versions."""
    errors = validate_shape(BATCH, batch)
    if errors:
        raise InvalidRequest("invalid edit envelope", records=errors)
    for index, edit in enumerate(batch["edits"]):
        if edit["collection"] not in ("source_reviews", "source_issues"):
            errors.append(f"edits/{index}: source review batches carry source_reviews and source_issues only")
        body = edit.get("body")
        if edit["collection"] == "source_reviews" and isinstance(body, dict):
            for field in ("source_refs", "anchor_refs"):
                for ref in body.get(field, []) if isinstance(body.get(field), list) else []:
                    if not isinstance(ref, dict) or not {"collection", "id", "version"} <= set(ref):
                        continue
                    head = db.head(ref["collection"], ref["id"]) if ref["collection"] in ("sources", "anchors") \
                        else None
                    if head is None or head.retired or head.version != ref["version"]:
                        errors.append(f"edits/{index}: {field} entry {ref['id']} must pin the live version")
    if errors:
        raise InvalidRequest("source review rejected", records=errors)
    return accept(db, request_id=batch["request_id"], request_digest=digest(batch), packet_id=batch["packet_id"],
                  edits=batch["edits"], command="source_review")


__all__ = ["anchor_sources", "capture_sources", "citations", "declarations", "discover", "media_type",
           "resolve_anchor", "review_sources", "uncomment"]
